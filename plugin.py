#!/usr/bin/env python3
# -*- coding: utf-8 -*-


# Author: ErwanBCN,
# Version:    0.0.1: alpha...


"""
<plugin key="ZZ-VMCDF" name="RONELABS - VMC DF Control" author="ErwanBCN" version="0.0.1" externallink="https://ronelabs.com">
    <description>
        <h2>VMC DF Control</h2><br/>
        Easily implement in Domoticz a VMC DF Control<br/>
        <h3>Set-up and Configuration</h3>
    </description>
    <params>
        <param field="Mode1" label="Humidity sensor (CSV List of idx)" width="400px" required="true" default=""/>
        <param field="Mode2" label="offsets (same order, 0 if none)" width="400px" required="true" default=""/>
        <param field="Mode3" label="High speed relay(CSV List of idx)" width="50px" required="true" default=""/>
        <param field="Mode4" label="Low humidity threshold (e.g. 70)" width="50px" required="true" default="70"/>
        <param field="Mode5" label="High humidity threshold (e.g. 85)" width="50px" required="true" default="90"/>
        <param field="Mode6" label="Logging Level" width="200px">
            <options>
                <option label="Normal" value="Normal"  default="true"/>
                <option label="Verbose" value="Verbose"/>
                <option label="Debug - Python Only" value="2"/>
                <option label="Debug - Basic" value="62"/>
                <option label="Debug - Basic+Messages" value="126"/>
                <option label="Debug - Connections Only" value="16"/>
                <option label="Debug - Connections+Queue" value="144"/>
                <option label="Debug - All" value="-1"/>
            </options>
        </param>
    </params>
</plugin>
"""
# ----------------------------- Imports -----------------------------
import json
import urllib
import urllib.parse as parse
import urllib.request as request
from datetime import datetime
import time
import Domoticz

try:
    from Domoticz import Devices, Parameters
except ImportError:
# Permet d'éviter des erreurs à l'analyse statique
    pass

# ----------------------------- Plugin -----------------------------

class deviceparam:

    def __init__(self, unit, nvalue, svalue):
        self.unit = unit
        self.nvalue = nvalue
        self.svalue = svalue
        self.debug = False


class BasePlugin:
    def __init__(self):
        self.debug = False
        self.loglevel = "Normal"

        self.hum_idxs = []
        self.hum_offsets = []
        self.relay_idx = None
        self.low_th = 55.0
        self.high_th = 65.0

        self.last_auto_state_on = False  # état calculé en mode auto (hystérésis)
        self.force_mode = False          # état du bouton Forcé (Unit 3)
        self.last_values = {
            'avg_hum': None,
        }

    # -------------- Life cycle --------------

    def onStart(self):
        Domoticz.Log("onStart called")
        # setup the appropriate logging level
        try:
            debuglevel = int(Parameters["Mode6"])
        except ValueError:
            debuglevel = 0
            self.loglevel = Parameters["Mode6"]
        if debuglevel != 0:
            self.debug = True
            Domoticz.Debugging(debuglevel)
            DumpConfigToLog()
            self.loglevel = "Verbose"
        else:
            self.debug = False
            Domoticz.Debugging(0)

        # Paramètres
        self.hum_idxs = parseCSV_to_ints(Parameters.get("Mode1", ""))
        self.hum_offsets = parseCSV_to_floats(Parameters.get("Mode2", ""))

        try:
            self.relay_idx = int(float(Parameters.get("Mode3", 0))) or None
        except Exception:
            self.relay_idx = None

        try:
            self.low_th = float(Parameters.get("Mode4", 55))
        except Exception:
            self.low_th = 55.0
        try:
            self.high_th = float(Parameters.get("Mode5", 65))
        except Exception:
            self.high_th = 65.0

        # Sanity: swap si inversés
        if self.low_th > self.high_th:
            Domoticz.Error(f"Inverted thresholds detected (low {self.low_th} > high {self.high_th}) — inversion.")
            self.low_th, self.high_th = self.high_th, self.low_th

        # Créer les devices enfants (re-numérotés)
        created = []
        if 1 not in Devices:
            Domoticz.Device(Unit=1, Name="Avg Humidity", TypeName="Humidity", Used=1).Create()
            created.append(1)
        if 2 not in Devices:
            Domoticz.Device(Unit=2, Name="Boost", Type=243, Subtype=19, Used=1).Create()  # General / Text sensor
            created.append(2)
        if 3 not in Devices:
            Domoticz.Device(Unit=3, Name="Manual", TypeName="Switch", Used=1).Create()
            created.append(3)

        for u in created:
            Devices[u].Update(nValue=0, sValue="")

        # Set domoticz heartbeat to x s between 5 to 20 max
        Domoticz.Heartbeat(20)

        # Lecture initiale + maj état
        self.refresh_and_act()


    def onStop(self):
        Domoticz.Log("onStop called")
        Domoticz.Debugging(0)

    def onCommand(self, Unit, Command, Level, Color):
        Domoticz.Log(f"VMC-DF: onCommand Unit={Unit} Command={Command} Level={Level}")
        cmd = (Command or '').strip().lower()
        if Unit == 3:  # Bouton Forcé
            if cmd == 'on':
                self.force_mode = True
                Devices[3].Update(nValue=1, sValue="On")
            elif cmd == 'off':
                self.force_mode = False
                Devices[3].Update(nValue=0, sValue="Off")
            # Appliquer immédiatement
            self.apply_control()
        else:
            pass

    def onHeartbeat(self):
        Domoticz.Debug("onHeartbeat called")

        # refresh values and act
        self.refresh_and_act()


    # OTHER DEF -------------------------------------------------------------------------------------------------------

    # -------------- Logique principale --------------

    def refresh_and_act(self):
        avg_hum = self.compute_avg_humidity()

        # Mettre à jour les devices enfants
        if avg_hum is not None:
            hum_int = int(round(avg_hum))
            Devices[1].Update(nValue=hum_int, sValue=str(hum_int))

        self.last_values['avg_hum'] = avg_hum

        # Contrôle du relais
        self.apply_control()

    def apply_control(self):
        mode_label = "Forcé" if self.force_mode else "Auto"

        target_on = False
        if self.force_mode:
            target_on = True
        else:
            # Auto: hystérésis sur l'humidité moyenne
            avg_hum = self.last_values.get('avg_hum')
            if avg_hum is None:
                # Pas de mesure -> ne rien changer
                self.post_state(mode_label, None)
                return

            if avg_hum >= self.high_th:
                target_on = True
            elif avg_hum <= self.low_th:
                target_on = False
            else:
                # Zone neutre: on garde l'état précédent
                target_on = self.last_auto_state_on

        # Appliquer si changement
        if self.force_mode is False:
            self.last_auto_state_on = target_on

        applied = self.switch_relay(target_on)
        self.post_state(mode_label, target_on if applied else None)

    def post_state(self, mode_label, target_on):
        if target_on is None:
            txt = f"{mode_label} — état inchangé"
        else:
            txt = f"{mode_label} — {'ON' if target_on else 'OFF'}"
        Devices[2].Update(nValue=0, sValue=txt)

    # -------------- Mesures --------------

    def compute_avg_humidity(self):
        vals = []
        for i, idx in enumerate(self.hum_idxs):
            dev = self.get_device_by_idx(idx)
            if not dev:
                continue
            val = None
            if 'Humidity' in dev:
                try:
                    val = float(dev['Humidity'])
                except Exception:
                    val = None
            elif 'Data' in dev:
                # ex: "53 %"
                try:
                    d = str(dev['Data']).strip()
                    if d.endswith('%'):
                        val = float(d[:-1].strip())
                except Exception:
                    val = None
            if val is not None:
                off = self.hum_offsets[i] if i < len(self.hum_offsets) else 0.0
                vals.append(val + off)
        if vals:
            return sum(vals) / len(vals)
        return None

    # -------------- Relais --------------

    def switch_relay(self, on):
        if not self.relay_idx:
            Domoticz.Error("Relay IDX not configured (Mode3)")
            return False
        cmd = 'On' if on else 'Off'
        res = DomoticzAPI(f"type=command&param=switchlight&idx={self.relay_idx}&switchcmd={cmd}")
        if not res:
            Domoticz.Error("Relay command failure")
            return False
        return True

    # -------------- get_device_by_idx --------------

    def get_device_by_idx(self, idx):
        res = DomoticzAPI(f"type=devices&rid={idx}")
        if res and 'result' in res and len(res['result']) > 0:
            return res['result'][0]
        Domoticz.Error(f"Device idx {idx} introuvable")
        return None

    # -------------- Write Log --------------

    def WriteLog(self, message, level="Normal"):

        if self.loglevel == "Verbose" and level == "Verbose":
            Domoticz.Log(message)
        elif level == "Normal":
            Domoticz.Log(message)


# Plugin utility functions ---------------------------------------------------

def DomoticzAPI(APICall):
    resultJson = None
    url = f"http://127.0.0.1:8080/json.htm?{parse.quote(APICall, safe='&=')}"

    try:
        Domoticz.Debug(f"Domoticz API request: {url}")
        req = request.Request(url)
        response = request.urlopen(req)

        if response.status == 200:
            resultJson = json.loads(response.read().decode('utf-8'))
            if resultJson.get("status") == "ERR":
                Domoticz.Error(f"Domoticz API returned an error: status = {resultJson.get('status')}")
                resultJson = None
        else:
            Domoticz.Error(f"Domoticz API: HTTP error = {response.status}")

    except urllib.error.HTTPError as e:
        Domoticz.Error(f"HTTP error calling '{url}': {e}")
    except urllib.error.URLError as e:
        Domoticz.Error(f"URL error calling '{url}': {e}")
    except json.JSONDecodeError as e:
        Domoticz.Error(f"JSON decoding error: {e}")
    except Exception as e:
        Domoticz.Error(f"Error calling '{url}': {e}")

    return resultJson


def parseCSV_to_floats(csv_str):
    out = []
    if not csv_str:
        return out
    for v in csv_str.split(','):
        v = v.strip()
        if v == '':
            continue
        try:
            out.append(float(v))
        except ValueError:
            Domoticz.Error(f"Skipping non-numeric value in CSV: {v}")
    return out


def parseCSV_to_ints(csv_str):
    out = []
    if not csv_str:
        return out
    for v in csv_str.split(','):
        v = v.strip()
        if v == '':
            continue
        try:
            out.append(int(float(v)))
        except ValueError:
            Domoticz.Error(f"Skipping non-integer value in CSV: {v}")
    return out

# Generic helper functions ---------------------------------------------------

def DumpConfigToLog():
    for x in Parameters:
        if Parameters[x] != "":
            Domoticz.Debug("'" + x + "':'" + str(Parameters[x]) + "'")
    Domoticz.Debug("Device count: " + str(len(Devices)))
    for x in Devices:
        Domoticz.Debug("Device:           " + str(x) + " - " + str(Devices[x]))
        Domoticz.Debug("Device ID:       '" + str(Devices[x].ID) + "'")
        Domoticz.Debug("Device Name:     '" + Devices[x].Name + "'")
        Domoticz.Debug("Device nValue:    " + str(Devices[x].nValue))
        Domoticz.Debug("Device sValue:   '" + Devices[x].sValue + "'")
        Domoticz.Debug("Device LastLevel: " + str(Devices[x].LastLevel))
    return

# Glue - Plugin functions ----------------------------------------------------------------------------------------------

global _plugin
_plugin = BasePlugin()


def onStart():
    global _plugin
    _plugin.onStart()


def onStop():
    global _plugin
    _plugin.onStop()


def onCommand(Unit, Command, Level, Color):
    global _plugin
    _plugin.onCommand(Unit, Command, Level, Color)


def onHeartbeat():
    global _plugin
    _plugin.onHeartbeat()

# End--------------------------------------------------------------- ---------------------------------------------------