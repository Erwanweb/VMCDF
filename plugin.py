#!/usr/bin/env python3
# -*- coding: utf-8 -*-


# Author: ErwanBCN,
# Version:    0.0.1: alpha...
# Version:    1.0.1: beta...


"""
<plugin key="ZZ-VMCDF" name="RONELABS - VMC DF Control" author="ErwanBCN" version="1.0.1" externallink="https://ronelabs.com">
    <description>
        <h2>VMC DF Control V1.0.1</h2><br/>
        Easily implement in Domoticz a VMC DF Control<br/>
        <h3>Set-up and Configuration</h3>
    </description>
    <params>
        <param field="Username" label="Outdoor Temp/Hum sensor (CSV List of idx)" width="400px" required="true" default=""/>
        <param field="Password" label="Indoor Temp/Hum sensor (CSV List of idx)" width="400px" required="true" default=""/>
        <param field="Mode1" label="Indoor wet rooms Humidity sensor (CSV List of idx)" width="400px" required="true" default=""/>
        <param field="Mode2" label="offsets wet rooms Humidity sensor (same order, 0 if none)" width="400px" required="true" default=""/>
        <param field="Mode3" label="Boost relay (CSV List of idx)" width="50px" required="true" default=""/>
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
import math
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

        self.outdoor_idxs = []
        self.indoor_idxs = []
        self.hum_idxs = []
        self.hum_offsets = []
        self.relay_idx = None
        self.low_th = 55.0
        self.high_th = 65.0

        self.last_auto_state_on = False
        self.force_mode = False
        self.last_values = {
            'avg_hum': None,
        }
        # Td-gate memory for anti-chatter
        self._last_td_gate = None

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
        self.outdoor_idxs = parseCSV_to_ints(Parameters.get("Username", ""))
        self.indoor_idxs = parseCSV_to_ints(Parameters.get("Password", ""))
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
        # Récupère toutes les humidités corrigées, calcule la moyenne pour affichage
        hum_vals = self.compute_hum_values()
        avg_hum = sum(hum_vals) / len(hum_vals) if hum_vals else None

        # Met à jour le device Humidity (format plugin: nValue=HUM, sValue=STATUS)
        if avg_hum is not None:
            hum_int = int(round(avg_hum))
            status = self.get_hum_status(hum_int)
            Devices[1].Update(nValue=hum_int, sValue=str(status))
        else:
            Devices[1].Update(nValue=0, sValue="0")

        self.last_values['avg_hum'] = avg_hum
        self.last_values['hum_list'] = hum_vals

        # --- Td-based bivalence computations ---
        try:
            T_ext, RH_ext = avg_T_RH_from_idxs(self.outdoor_idxs, self.get_device_by_idx)
            T_int, RH_int = avg_T_RH_from_idxs(self.indoor_idxs, self.get_device_by_idx)
        except Exception:
            T_ext = RH_ext = T_int = RH_int = None

        Td_ext = dew_point_celsius(T_ext, RH_ext) if (T_ext is not None and RH_ext is not None) else None
        Td_target = dew_point_celsius(T_int, RH_int) if (T_int is not None and RH_int is not None) else None

        # Hystérésis Td auto depuis Low/High (0.5–2.0°C)
        try:
            low = float(Parameters.get("Mode4", 55))
            high = float(Parameters.get("Mode5", 65))
            td_hyst = max(0.5, min(2.0, (high - low) / 10.0))
        except Exception:
            td_hyst = 1.0

        self.last_values.update({
            "T_ext": T_ext, "RH_ext": RH_ext,
            "T_int": T_int, "RH_int": RH_int,
            "Td_ext": Td_ext, "Td_target": Td_target,
            "td_hyst": td_hyst
        })

        Domoticz.Debug(
            f"-----DEBUG : Avg: T_int={self.last_values.get('T_int'):.1f}°C "
            f"RH_int={self.last_values.get('RH_int'):.1f}% | "
            f"T_ext={self.last_values.get('T_ext'):.1f}°C RH_ext={self.last_values.get('RH_ext'):.1f}%"
        )
        Domoticz.Debug(
            f"-----DEBUG : Td_ext={self.last_values.get('Td_ext'):.1f}°C Td_target={self.last_values.get('Td_target'):.1f}°C td_hyst={self.last_values.get('td_hyst'):.1f}°C "
        )

        # Contrôle du relais
        self.apply_control()

    def apply_control(self):
        mode_label = "Forcé" if self.force_mode else "Auto"

        target_on = False
        if self.force_mode:
            target_on = True
        else:
            hum_vals = self.last_values.get('hum_list') or []
            if not hum_vals:
                # Pas de mesure -> ne rien changer
                self.post_state(mode_label, None)
                return

            Td_ext = self.last_values.get("Td_ext")
            Td_target = self.last_values.get("Td_target")
            td_hyst = self.last_values.get("td_hyst", 1.0)

            if Td_ext is None or Td_target is None:
                # Nouvelle logique BOOST d’origine
                if any(v >= self.high_th for v in hum_vals):
                    target_on = True
                elif all(v <= self.low_th for v in hum_vals):
                    target_on = False
                else:
                    target_on = self.last_auto_state_on
            else:
                td_hi = Td_target + td_hyst
                td_lo = Td_target - td_hyst

                eps = 0.2  # petite marge anti-arrondi, ajuste 0.1–0.3 si besoin

                if self._last_td_gate is None:
                    # Initialisation avec tie-break vers HUMIDE
                    if Td_ext < (Td_target - eps):
                        gate = 'SEC'
                    elif Td_ext > (Td_target + eps):
                        gate = 'HUMIDE'
                    else:
                        gate = 'HUMIDE'  # égalité -> HUMIDE
                else:
                    gate = self._last_td_gate
                    # Hystérésis de bascule
                    if gate == 'SEC' and Td_ext >= td_hi:
                        gate = 'HUMIDE'
                    elif gate == 'HUMIDE' and Td_ext <= td_lo:
                        gate = 'SEC'

                self._last_td_gate = gate

                if gate == 'SEC':
                    # OK pour boosts humidité
                    if any(v >= self.high_th for v in hum_vals):
                        target_on = True
                    elif all(v <= self.low_th for v in hum_vals):
                        target_on = False
                    else:
                        target_on = self.last_auto_state_on
                else:
                    # EXT HUMIDE — enclenchement plus dur (+5), et "min" relevé à la RH mini atteignable (liée à Td_ext)
                    T_int = self.last_values.get("T_int")
                    Td_ext = self.last_values.get("Td_ext")

                    # HR mini physiquement atteignable à T_int avec un air neuf de Td_ext
                    if (T_int is not None) and (Td_ext is not None):
                        rh_floor = rh_from_t_and_tdew(T_int, Td_ext)  # p.ex. 26°C & Td_ext=20°C → ≈ 60%
                    else:
                        rh_floor = None

                    # Seuil d'arrêt effectif : ne pas viser sous la physique
                    if rh_floor is not None:
                        low_eff = max(self.low_th, rh_floor)
                    else:
                        low_eff = self.low_th

                    # Règles
                    if any(v >= (self.high_th + 5) for v in hum_vals):
                        target_on = True
                    elif all(v <= low_eff for v in hum_vals):
                        target_on = False
                    else:
                        target_on = self.last_auto_state_on

        if self.force_mode is False:
            self.last_auto_state_on = target_on

        applied = self.switch_relay(target_on)
        self.post_state(mode_label, target_on if applied else None)

    def post_state(self, mode_label, target_on):
        if target_on is None:
            txt = f"{mode_label} — état inchangé"
        else:
            txt = f"{mode_label} — {'ON' if target_on else 'OFF'}"

        # Ajout d’un append Td propre sans changer l’ordre
        Td_ext = self.last_values.get("Td_ext")
        Td_target = self.last_values.get("Td_target")
        gate = self._last_td_gate or "-"
        if Td_ext is not None and Td_target is not None:
            try:
                Domoticz.Debug(
                    f"-----DEBUG : T_ext={Td_ext:.1f}°C / RH_ext={self.last_values.get('RH_ext'):.1f}% | "
                    f"T_int={self.last_values.get('T_int'):.1f}°C / RH_int={self.last_values.get('RH_int'):.1f}% | "
                    f"Td_ext={Td_ext:.1f}°C / Td_int={Td_target:.1f}°C | Gate={gate}"
                )
                txt += f" | Ext {gate}"
            except Exception:
                pass

        Devices[2].Update(nValue=0, sValue=txt)

    # -------------- Mesures --------------
    def compute_hum_values(self):
        """Retourne la liste des humidités corrigées (offsets appliqués)."""
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
                try:
                    d = str(dev['Data']).strip()
                    if d.endswith('%'):
                        val = float(d[:-1].strip())
                except Exception:
                    val = None
            if val is not None:
                off = self.hum_offsets[i] if i < len(self.hum_offsets) else 0.0
                vals.append(val + off)
        return vals if vals else []

    def get_hum_status(self, hum_int):
        try:
            if hum_int is None:
                return 0
            if hum_int <= 50:
                return 2
            if hum_int >= 75:
                return 3
            return 1
        except Exception:
            return 0

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

# --- Psychrometric helpers (Magnus-Tetens) -----------------------------------

def dew_point_celsius(T_c, RH_pct):
    """Return dew point (°C) from dry-bulb T (°C) and relative humidity (%)."""
    try:
        T = float(T_c);
        RH = max(0.1, min(100.0, float(RH_pct)))
    except Exception:
        return None
    a, b = 17.62, 243.12
    gamma = (a * T) / (b + T) + math.log(RH / 100.0)
    return (b * gamma) / (a - gamma)

def rh_from_t_and_tdew(T_c, Td_c):
    """Retourne RH (%) à T (°C) sachant Td (°C), via Magnus."""
    try:
        T = float(T_c); Td = float(Td_c)
    except Exception:
        return None
    a, b = 17.62, 243.12
    rh = 100.0 * math.exp((a*Td)/(b+Td) - (a*T)/(b+T))
    return max(0.0, min(100.0, rh))

def avg_T_RH_from_idxs(idx_list, get_device_fn):
    """Return (T_avg, RH_avg) from Domoticz Temp+Humidity devices (CSV idx list)."""
    Ts, RHs = [], []
    for idx in idx_list or []:
        dev = get_device_fn(idx)
        if not dev:
            continue
        t = None
        if 'Temp' in dev:
            try:
                t = float(dev['Temp'])
            except Exception:
                t = None
        elif 'Data' in dev and 'C' in str(dev['Data']):
            try:
                part = str(dev['Data']).split(',')[0]
                t = float(part.strip().split(' ')[0])
            except Exception:
                t = None
        if t is not None:
            Ts.append(t)
        h = None
        if 'Humidity' in dev:
            try:
                h = float(dev['Humidity'])
            except Exception:
                h = None
        elif 'Data' in dev and '%' in str(dev['Data']):
            try:
                part = str(dev['Data']).split(',')[1]
                h = float(part.strip().replace('%', '').strip())
            except Exception:
                h = None
        if h is not None:
            RHs.append(h)
    T_avg = sum(Ts) / len(Ts) if Ts else None
    RH_avg = sum(RHs) / len(RHs) if RHs else None
    return T_avg, RH_avg

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