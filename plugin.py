#!/usr/bin/env python3
# -*- coding: utf-8 -*-


# Author: ErwanBCN,
# Version:    0.0.1: alpha...
# Version:    1.0.1: beta...
# Version:    1.0.2: first valid...


"""
<plugin key="ZZ-VMCDF" name="RONELABS - VMC DF Control" author="ErwanBCN" version="1.0.2" externallink="https://ronelabs.com">
    <description>
        <h2>VMC DF Control V1.0.2</h2><br/>
        Easily implement in Domoticz a VMC DF Inteliggent Control<br/>
        <h3>Set-up and Configuration</h3>
    </description>
    <params>
        <param field="Username" label="Outdoor Temp/Hum sensors (CSV List of idx)" width="400px" required="true" default=""/>
        <param field="Password" label="Normal rooms Temp/Hum sensors (CSV List of idx)" width="400px" required="true" default=""/>
        <param field="Mode1" label="Wet rooms Temp/Hum sensors (CSV List of idx)" width="400px" required="true" default=""/>
        <param field="Mode2" label="offsets Wet rooms Hum. sensors (same order, 0 if none)" width="400px" required="true" default=""/>
        <param field="Mode3" label="Boost relay (CSV List of idx)" width="50px" required="true" default=""/>
        <param field="Mode4" label="Presence sensors (CSV List of idx)" width="400px" required="false" default=""/>
        <param field="Mode5" label="Params(expert) : Timer(Mins),RH↓,RH↑,ΔTd-DRY,ΔTd-ON,ΔTd-OFF " width="400px" required="true" default="60,55,75,20,10,5"/>
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
from datetime import datetime, timedelta
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

class BasePlugin:
    def __init__(self):
        self.debug = False
        self.loglevel = "Normal"

        now = datetime.now() # Time helper

        self.outdoor_idxs = []
        self.indoor_idxs = []
        self.hum_idxs = []
        self.hum_offsets = []
        self.relay_idx = None
        #self._last_td_gate = None
        self.Timer = 60
        self.TimerStartedTime = now
        self.TimerOn = False
        self.low_th = 55.0
        self.high_th = 75.0
        self._td_eps = 2.0  # marge 'nettement plus sec' pour gate SEC (°C)
        self._td_on = 1.0  # ΔTd pour enclenchement HUMIDE (°C)
        self._td_off = 0.5  # ΔTd pour arrêt HUMIDE (°C)

        self.last_auto_state_on = False
        self.force_mode = False
        self.last_values = {
            'avg_hum': None,
        }

        # Paramètres internes
        self._ext_offset = 2.0  # fallback HUMIDE : RH_ext + 2 %

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
            Domoticz.Error("NO Boost Relay in idx parameters")

        # splits experts parameters
        #params = parseCSV(Parameters["Mode5"])
        params = parseCSV_to_ints(Parameters.get("Mode5", ""))
        if len(params) == 6: # Timer(Min),L/H-HR,ΔTd-DRY,ΔTd-ON,ΔTd-OFF
            self.Timer = CheckParam("Timer(Min)", params[0], 60) # timer Auto
            self.low_th = CheckParam("RH↓ ", params[1], 55) # LOW Humidity threshold - DRY Mode
            self.high_th = CheckParam("RH↑ ", params[2], 75) # HIGH Humidity threshold - DRY Mode
            self._td_eps = CheckParam("ΔTd-DRY,", params[3], 30) # marge 'nettement plus sec' pour gate SEC (°C)
            self._td_eps = float(self._td_eps) /10
            self._td_on = CheckParam("ΔTd-ON", params[4], 10) # ΔTd pour enclenchement BOOST HUMIDE (°C) - WET Mode
            self._td_on = float(self._td_on) /10
            self._td_off = CheckParam("ΔTd-OFF", params[5], 5) # ΔTd pour arrêt BOOST HUMIDE (°C) - WET Mode
            self._td_off = float(self._td_off) /10
        else:
            Domoticz.Error("Error reading Experts (MODE 5) parameters")

        # Sanity: swap si inversés
        if self.low_th > self.high_th:
            Domoticz.Error(f"Inverted thresholds detected (low {self.low_th} > high {self.high_th}) — inversion.")
            self.low_th, self.high_th = self.high_th, self.low_th

        # Créer les devices enfants (re-numérotés)
        created = []
        if 1 not in Devices:
            Domoticz.Device(Unit=1, Name="Avg Wet Rooms", Type=82, Subtype=1, Used=1).Create()
            created.append(1)
        if 2 not in Devices:
            Domoticz.Device(Unit=2, Name="Info", Type=243, Subtype=19, Used=1).Create()  # General / Text sensor
            created.append(2)
        if 3 not in Devices:
            Options = {"LevelActions": "||",
                       "LevelNames": "Off|Auto|Timer|Forced",
                       "LevelOffHidden": "true",
                       "SelectorStyle": "0"}
            Domoticz.Device(Unit=3, Name="Thermostat Mode", TypeName="Selector Switch", Switchtype=18, Image=9, Options=Options, Used=1).Create()
            created.append(3)
        if 4 not in Devices:
            Domoticz.Device(Unit=4, Name="Avg Normal Rooms", Type=82, Subtype=1, Used=1).Create()
            created.append(1)

        for u in (1, 2, 3, 4):  
            if u in Devices:
                if u in (1, 4):  # Temp+Humidity -> format "t;h;status"
                    Devices[u].Update(nValue=0, sValue="0;0;0")
                elif u == 2:  # texte
                    Devices[u].Update(nValue=0, sValue="")
                elif u == 3:  # selector (Auto par défaut)
                    Devices[u].Update(nValue=1, sValue="10")

        # Set domoticz heartbeat to x s between 5 to 20 max
        Domoticz.Heartbeat(20)

        # Lecture initiale + maj état
        self.refresh_and_act()

    def onStop(self):
        Domoticz.Log("onStop called")
        Domoticz.Debugging(0)

    def onCommand(self, Unit, Command, Level, Color):
        Domoticz.Log(f"VMC-DF: onCommand Unit={Unit} Command={Command} Level={Level}")

        if Unit != 3:
            return

        # Normalise le Level
        try:
            lvl = int(Level)
        except Exception:
            # Certains envois "Set Level" sans Level exploitable → repli sur sValue
            try:
                lvl = int(Devices[3].sValue)
            except Exception:
                lvl = 10  # Auto par défaut

        now = datetime.now()

        if lvl == 10:  # Auto
            self.force_mode = False
            self.TimerOn = False
            Devices[3].Update(nValue=1, sValue="10")

        elif lvl == 20:  # Timer
            self.force_mode = True
            self.TimerStartedTime = now
            self.TimerOn = True
            # Assure-toi que self.Timer (minutes) est défini quelque part (ex: 15)
            if not hasattr(self, "Timer"):
                self.Timer = 30
            Devices[3].Update(nValue=1, sValue="20")

        elif lvl == 30:  # Forced
            self.force_mode = True
            self.TimerOn = False
            Devices[3].Update(nValue=1, sValue="30")

        else:  # Valeur inattendue → Auto
            self.force_mode = False
            self.TimerOn = False
            Devices[3].Update(nValue=1, sValue="10")

        # Appliquer immédiatement
        self.apply_control()

    def onHeartbeat(self):
        Domoticz.Debug("--------------DEBUG : onHeartbeat called")

        now = datetime.now()

        if self.TimerOn :
            if self.TimerStartedTime  + timedelta(minutes=self.Timer) <= now :
                self.force_mode = False
                self.TimerOn = False
                Devices[3].Update(nValue=1, sValue="10")
                
        # refresh values and act
        self.refresh_and_act()

    # OTHER DEF -------------------------------------------------------------------------------------------------------

    # -------------- Main Logic --------------
    def refresh_and_act(self):
        hum_vals = self.compute_hum_values()
        self.last_values['hum_list'] = hum_vals
        avg_hum = sum(hum_vals) / len(hum_vals) if hum_vals else None

        # Initialise
        T_ext = RH_ext = T_int = RH_int = None
        try:
            T_ext, RH_ext = avg_T_RH_from_idxs(self.outdoor_idxs, self.get_device_by_idx)
            T_int, RH_int = avg_T_RH_from_idxs(self.indoor_idxs, self.get_device_by_idx)
        except Exception:
            pass  # on garde None

        # Moyenne T et RH des pièces humides
        T_wet, _ = avg_T_RH_from_idxs(self.hum_idxs, self.get_device_by_idx)

        if 1 in Devices:
            if (T_wet is not None) and (avg_hum is not None):
                t_val = round(float(T_wet), 1)
                h_val = int(round(float(avg_hum)))
                status = self.get_hum_status(h_val)
                Devices[1].Update(nValue=0, sValue=f"{t_val:.1f};{h_val};{status}")
            else:
                Devices[1].Update(nValue=0, sValue="0;0;0")
            if self.debug and ((T_wet is None) or (avg_hum is None)):
                Domoticz.Debug("--------------DEBUG : Device 1: valeurs manquantes -> 0;0;0")

        # Moyenne T et RH des des pièces normales ---
        if 4 in Devices:
            if (T_int is not None) and (RH_int is not None):
                t_val = round(float(T_int), 1)
                h_val = int(round(float(RH_int)))
                status = self.get_hum_status(h_val)
                Devices[4].Update(nValue=0, sValue=f"{t_val:.1f};{h_val};{status}")
            else:
                Devices[4].Update(nValue=0, sValue="0;0;0")

        if self.debug and ((T_int is None) or (RH_int is None)):
            Domoticz.Debug("--------------DEBUG : Device 4: valeurs manquantes -> 0;0;0")

        Td_ext = dew_point_celsius(T_ext, RH_ext) if (T_ext is not None and RH_ext is not None) else None
        Td_target = dew_point_celsius(T_int, RH_int) if (T_int is not None and RH_int is not None) else None

        self.last_values.update({
            "T_ext": T_ext, "RH_ext": RH_ext,
            "T_int": T_int, "RH_int": RH_int,
            "Td_ext": Td_ext, "Td_target": Td_target,
        })

        # Td pièces humides
        try:
            td_rooms = compute_room_td_list(self)
        except Exception:
            td_rooms = []
        self.last_values["td_rooms"] = td_rooms

        self.apply_control()

    def apply_control(self):
        if self.TimerOn :
            mode_label = "Timer"
        else :
            mode_label = "Forced" if self.force_mode else "Auto"

        target_on = False
        if self.force_mode:
            target_on = True
        else:
            hum_vals = self.last_values.get('hum_list') or []
            if not hum_vals:
                self.post_state(mode_label, None)
                return

            Td_ext = self.last_values.get("Td_ext")
            Td_target = self.last_values.get("Td_target")

            # --- Gate SEC/HUMIDE (simplifié) ---
            eps = getattr(self, "_td_eps", 0.5)
            if (Td_ext is None) or (Td_target is None):
                gate = self._last_td_gate or "HUMIDE"
            else:
                gate = 'SEC' if (Td_ext < (Td_target - eps)) else 'HUMIDE'
                self._last_td_gate = gate
            self.last_values["gate"] = gate  # mémorise pour l'affichage Info

            if gate == 'SEC':
                Td_ext = self.last_values.get("Td_ext")
                td_rooms = self.last_values.get("td_rooms") or []

                # Si Td indisponible -> repli HR classique (min/max)
                if (Td_ext is None) or (not td_rooms):
                    if any(v >= self.high_th for v in hum_vals):
                        target_on = True
                    elif all(v <= self.low_th for v in hum_vals):
                        target_on = False
                    else:
                        target_on = self.last_auto_state_on

                    # DEBUG fallback HR
                    if self.debug:
                        try:
                            lst_hum = ", ".join(f"{h:.1f}" for h in hum_vals) if hum_vals else "-"
                            Domoticz.Debug(
                                f"--------------DEBUG : DRY MODE (fallback HR) | High={self.high_th:.1f} Low={self.low_th:.1f} "
                                f"| RH_rooms=[{lst_hum}] | Boost -> "
                                f"{'ON' if target_on else 'OFF' if target_on is False else 'HOLD'}"
                            )
                        except Exception:
                            pass

                else:
                    # HR min/max + garde-fou ΔTd (réutilise _td_on/_td_off)
                    td_on = getattr(self, "_td_on", 1.0)  # ON si Td_room - Td_ext ≥ +1.0°C
                    td_off = getattr(self, "_td_off", 0.5)  # OFF si Td_room - Td_ext ≤ 0.5°C (toutes)

                    n = min(len(hum_vals), len(td_rooms))

                    any_high_and_potential = any(
                        (hum_vals[i] >= self.high_th) and ((td_rooms[i] - Td_ext) >= td_on)
                        for i in range(n)
                    )
                    all_low_or_nopotential = all(
                        (hum_vals[i] <= self.low_th) or ((td_rooms[i] - Td_ext) <= td_off)
                        for i in range(n)
                    )

                    if any_high_and_potential:
                        target_on = True
                    elif all_low_or_nopotential:
                        target_on = False
                    else:
                        target_on = self.last_auto_state_on

                    # DEBUG complet SEC
                    if self.debug:
                        try:
                            lst_td = ", ".join(f"{v:.1f}" for v in td_rooms) if td_rooms else "-"
                            lst_gap = ", ".join(f"{(td_rooms[i] - Td_ext):+.1f}" for i in range(n)) if n > 0 else "-"
                            lst_hum = ", ".join(f"{h:.1f}" for h in hum_vals[:n]) if n > 0 else "-"
                            Domoticz.Debug(
                                f"--------------DEBUG : DRY MODE | High= {self.high_th:.1f} Low= {self.low_th:.1f} "
                                f"| On@High & Δ≥ {td_on:+.1f} / Off@All(Low or Δ≤ {td_off:+.1f}) "
                                f"| Td_ext= {Td_ext:.1f} °C | Td_rooms= [{lst_td}] | ΔTd_gaps=[{lst_gap}] "
                                f"| RH_rooms= [{lst_hum}] | Boost -> "
                                f"{'ON' if target_on else 'OFF' if target_on is False else 'HOLD'}"
                            )
                        except Exception:
                            pass

            else:
                # --- HUMIDE pilotage par ΔTd ---
                Td_ext = self.last_values.get("Td_ext")
                td_rooms = self.last_values.get("td_rooms") or []

                if Td_ext is None or not td_rooms:
                    RH_ext = self.last_values.get("RH_ext")
                    low_eff = self.low_th if RH_ext is None else min(100.0, max(0.0, RH_ext + getattr(self,"_ext_offset",2.0)))
                    if any(v >= (self.high_th + 5) for v in hum_vals):
                        target_on = True
                    elif all(v <= low_eff for v in hum_vals):
                        target_on = False
                    else:
                        target_on = self.last_auto_state_on
                else:
                    td_on = getattr(self, "_td_on", 1)
                    td_off = getattr(self, "_td_off", 0.5)

                    any_need_on = any((tdp is not None) and (tdp >= Td_ext + td_on) for tdp in td_rooms)
                    all_ok_off = all((tdp is not None) and (tdp <= Td_ext + td_off) for tdp in td_rooms)

                    if any_need_on:
                        target_on = True
                    elif all_ok_off:
                        target_on = False
                    else:
                        target_on = self.last_auto_state_on

                # DEBUG complet HUMIDE
                if self.debug:
                    try:
                        lst_td = ", ".join(f"{v:.1f}" for v in td_rooms) if td_rooms else "-"
                        lst_gap = ", ".join(f"{(td - Td_ext):+.1f}" for td in td_rooms) if td_rooms else "-"
                        Domoticz.Debug(
                            f"--------------DEBUG : WET MODE | On@≥ {td_on:+.1f} & Off@≤ {td_off:+.1f} "
                            f"| ΔTd: Td_ext(°C)= {Td_ext:.1f}°C | Td_rooms(°C)= [{lst_td}] | ΔTd_gaps(°C)= [{lst_gap}] "
                            f"| Boost -> {'ON' if target_on else 'OFF' if target_on is False else 'HOLD'}"
                        )
                    except Exception:
                        pass

        if self.force_mode is False:
            self.last_auto_state_on = target_on

        applied = self.switch_relay(target_on)
        self.post_state(mode_label, target_on if applied else None)

    def post_state(self, mode_label, target_on):
        # Affiche SEC/HUMIDE dans Info quand on est en Auto
        gate = self.last_values.get("gate")
        gate_tag = ""
        if mode_label == "Auto" and gate:
            gate_tag = f" ({'DRY' if gate == 'SEC' else 'WET'})"

        if target_on is None:
            txt = f"{mode_label} {gate_tag} — HOLD"
        else:
            txt = f"{mode_label} {gate_tag} — Boost {'ON' if target_on else 'OFF'}"

        Devices[2].Update(nValue=0, sValue=txt)
        Domoticz.Debug(
            f"--------------DEBUG : {mode_label} {gate_tag} | Boost -> {'ON' if target_on else 'OFF' if target_on is False else 'HOLD'}")

    # -------------- Mesures --------------
    def compute_hum_values(self):
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
            if hum_int >= 70:
                return 3
            return 1
        except Exception:
            return 0

    # -------------- Relais --------------
    def switch_relay(self, on):
        if not self.relay_idx:
            Domoticz.Error("Relay IDX not configured (Mode3)")
            return False

        desired = 'On' if on else 'Off'

        # 1) Lire l’état actuel du relais
        cur_state = None
        dev = DomoticzAPI(f"type=devices&rid={self.relay_idx}")
        try:
            if dev and 'result' in dev and len(dev['result']) > 0:
                d = dev['result'][0]
                # Cas standard: champ "Status" vaut "On"/"Off"
                cur_state = (d.get('Status') or '').strip()
                if not cur_state:
                    # Fallback: certains renvoient "Data" == "On"/"Off" ou "Set Level: 0/100"
                    data = (d.get('Data') or '').strip()
                    if data in ('On', 'Off'):
                        cur_state = data
                    else:
                        # Fallback ultime: nValue (1=On, 0=Off)
                        n = d.get('nValue')
                        if n is not None:
                            try:
                                cur_state = 'On' if int(n) == 1 else 'Off'
                            except Exception:
                                cur_state = None
        except Exception as e:
            Domoticz.Error(f"Relay state read error: {e}")
            cur_state = None

        # 2) Si déjà dans le bon état, ne rien envoyer
        if cur_state == desired:
            if self.debug:
                Domoticz.Debug(f"--------------DEBUG : Relay idx {self.relay_idx}: already {desired}, skipping command.")
            return True

        # 3) Envoyer la commande uniquement si nécessaire
        cmd = desired
        res = DomoticzAPI(f"type=command&param=switchlight&idx={self.relay_idx}&switchcmd={cmd}")
        if not res or str(res.get('status', '')).lower() != 'ok':
            Domoticz.Error(f"Relay command failure (idx {self.relay_idx}, cmd {cmd})")
            return False

        if self.debug:
            Domoticz.Debug(f"--------------DEBUG : Relay idx {self.relay_idx}: sent {cmd} (prev={cur_state or 'unknown'})")
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

# Psychrometric helpers (Magnus-Tetens) --------------------------------------------------------------------------------
def dew_point_celsius(T_c, RH_pct):
    """Return dew point (°C) from dry-bulb T (°C) and relative humidity (%)."""
    try:
        T = float(T_c); RH = max(0.1, min(100.0, float(RH_pct)))
    except Exception:
        return None
    a, b = 17.62, 243.12
    gamma = (a * T) / (b + T) + math.log(RH / 100.0)
    return (b * gamma) / (a - gamma)

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
                h = float(part.strip().replace('%','').strip())
            except Exception:
                h = None
        if h is not None:
            RHs.append(h)
    T_avg = sum(Ts)/len(Ts) if Ts else None
    RH_avg = sum(RHs)/len(RHs) if RHs else None
    return T_avg, RH_avg

def compute_room_td_list(self):
    def fmt1(x, unit=""):
        return "-" if x is None else f"{x:.1f}{unit}"

    Td_list = []
    T_int = self.last_values.get("T_int") or 21.0

    per_sensor_logs = []  # pour un récap en fin

    for i, idx in enumerate(self.hum_idxs):
        dev = self.get_device_by_idx(idx)
        if not dev:
            continue

        # --- RH pièce ---
        RH = None
        if 'Humidity' in dev:
            try:
                RH = float(dev['Humidity'])
            except Exception:
                RH = None
        elif 'Data' in dev:
            try:
                d = str(dev['Data']).strip()
                if d.endswith('%'):
                    RH = float(d[:-1].strip())
            except Exception:
                RH = None
        if RH is None:
            if self.debug:
                Domoticz.Debug(f"--------------DEBUG : ΔTd: idx={idx} RH=NA (pas de valeur) – ignoré")
            continue

        # --- OFFSET + clamp ---
        off = self.hum_offsets[i] if i < len(self.hum_offsets) else 0.0
        RH = max(0.0, min(100.0, RH + off))

        # --- Temp de la sonde (si dispo), sinon T_int ---
        T_room = None
        if 'Temp' in dev:
            try:
                T_room = float(dev['Temp'])
            except Exception:
                T_room = None
        if T_room is None:
            T_room = T_int

        Td = dew_point_celsius(T_room, RH)
        if Td is not None:
            Td_list.append(Td)

    return Td_list

# Plugin helpers & utility functions -----------------------------------------------------------------------------------

# Domoticz API  --------------------------------------------------------------------------------------------------------

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

# CSV and param Helpers ------------------------------------------------------------------------------------------------
def parseCSV_to_ints(s):
    return [int(x.strip()) for x in s.split(',') if x.strip().isdigit()]

def parseCSV_to_floats(s):
    out = []
    for x in s.split(','):
        try:
            out.append(float(x.strip()))
        except Exception:
            pass
    return out

def CheckParam(name, value, default):
    try:
        param = int(value)
    except ValueError:
        param = default
        Domoticz.Error("Parameter '{}' has an invalid value of '{}' ! defaut of '{}' is instead used.".format(name, value, default))
    return param

# Generic helper functions ---------------------------------------------------------------------------------------------

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