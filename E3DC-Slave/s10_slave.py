#!/usr/bin/env python3
"""Test runner: observes by default. --execute explicitly enables slave commands."""
import argparse,datetime,json,logging,os,signal,threading,time
from importlib import metadata
from pathlib import Path
from slave_policy import Controller,Settings,Reading
from power_limits import PowerLimits,PowerSettingsError

READ_REQUESTS = frozenset({
    'EMS_REQ_DERATE_AT_PERCENT_VALUE', 'EMS_REQ_DERATE_AT_POWER_VALUE',
    'EMS_REQ_INSTALLED_PEAK_POWER', 'EMS_REQ_EXT_SRC_AVAILABLE',
    'INFO_REQ_MAC_ADDRESS', 'INFO_REQ_SERIAL_NUMBER', 'EMS_REQ_GET_SYS_SPECS',
    'INFO_REQ_UTC_TIME', 'EMS_REQ_BAT_SOC', 'EMS_REQ_POWER_PV', 'EMS_REQ_POWER_ADD',
    'EMS_REQ_POWER_BAT', 'EMS_REQ_POWER_HOME', 'EMS_REQ_POWER_GRID',
    'EMS_REQ_POWER_WB_ALL', 'EMS_REQ_SELF_CONSUMPTION', 'EMS_REQ_AUTARKY',
    'EMS_REQ_GET_POWER_SETTINGS',
})

RSCP_ERROR_CODES = frozenset({
    'RSCP_ERR_NOT_HANDLED', 'RSCP_ERR_ACCESS_DENIED', 'RSCP_ERR_FORMAT',
    'RSCP_ERR_AGAIN', 'RSCP_ERR_OUT_OF_BOUNDS', 'RSCP_ERR_NOT_AVAILABLE',
    'RSCP_ERR_UNKNOWN_TAG', 'RSCP_ERR_ALREADY_IN_USE',
})

def event(name, **values):
    print(json.dumps({'event': name, 'time': datetime.datetime.now().astimezone().isoformat(),
                      **values}), flush=True)

def error_diagnostics(exc):
    """Nur Fehlertypen, errno und bekannte RSCP-Fehlercodes erfassen, keine Nutzdaten."""
    chain, seen = [], set()
    while isinstance(exc, BaseException) and id(exc) not in seen and len(chain) < 6:
        seen.add(id(exc))
        name = type(exc).__name__
        entry = {'type': name if len(name) <= 80 and name.isascii() and name.isidentifier() else 'Exception'}
        try:
            number = getattr(exc, 'errno', None)
        except Exception:
            number = None
        if type(number) is int:
            entry['errno'] = number
        if name == 'CommunicationError':
            try:
                arguments = exc.args
            except Exception:
                arguments = ()
            # pye3dc gibt dekodierte Fehlerantworten als CommunicationError(Code) weiter.
            if (type(arguments) is tuple and len(arguments) == 1
                    and type(arguments[0]) is str and arguments[0] in RSCP_ERROR_CODES):
                entry['rscp_error_code'] = arguments[0]
        chain.append(entry)
        exc = exc.__cause__ if exc.__cause__ is not None else exc.__context__
    return {'error_chain': chain, 'error_chain_truncated': isinstance(exc, BaseException)}

def installed_library_version():
    try:
        version = metadata.version('pye3dc')
    except Exception:
        return None
    if (isinstance(version, str) and 0 < len(version) <= 80 and version[0].isdigit()
            and all(c in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.!+_-' for c in version)):
        return version
    return None

class ReadRetryMixin:
    def sendRequest(self, request, retries=0, keepAlive=False):
        tag = getattr(request[0], 'name', request[0])
        attempts = 3 if tag in READ_REQUESTS else 1
        for attempt in range(attempts):
            try:
                # Ein Client behält seine Sitzung auch über Bibliotheksaufrufe mit False-Default.
                # Nur Fehler- und Abschlussbehandlung schließen; SET bleibt ein Einzelversuch.
                return super().sendRequest(request, retries=0, keepAlive=True)
            except self.communication_errors as exc:
                if attempt + 1 == attempts:
                    raise
                event('rscp_read_retry', request=tag, next_attempt=attempt + 2, **error_diagnostics(exc))
                self.disconnect()
                time.sleep(0.5)

def run():
    ap=argparse.ArgumentParser();ap.add_argument('--config',required=True);ap.add_argument('--execute',action='store_true');a=ap.parse_args()
    config=json.loads(Path(a.config).read_text(encoding='utf-8-sig'));settings=Settings(**config.get('controller',{}))
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    # Linux process lock, held for the full lifetime; the original script must be stopped separately.
    import fcntl
    lock=open(str(Path(a.config).resolve())+'.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    from e3dc import E3DC, SendError
    from e3dc._rscpTags import RscpType
    import paho.mqtt.client as mqtt
    event('runtime_diagnostics', execute=a.execute, diagnostics='rscp_persistent_session_v3',
          pye3dc_version=installed_library_version())
    def secret(name):
        value=os.environ.get(name)
        if not value:raise ValueError('required environment variable missing: '+name)
        return value
    class SingleAttemptE3DC(ReadRetryMixin, E3DC):
        communication_errors = (SendError,)
    s10=SingleAttemptE3DC(E3DC.CONNECT_LOCAL,ipAddress=config['slave_host'],username=secret('S10_USER'),password=secret('S10_PASSWORD'),key=secret('S10_RSCP_KEY'),configuration={})
    if a.execute:
        charge=getattr(s10,'maxBatChargePower',None);discharge=getattr(s10,'maxBatDischargePower',None)
        if not isinstance(charge,(int,float)) or not isinstance(discharge,(int,float)) or charge<=0 or discharge<=0:
            raise ValueError('hardware power limits unavailable; remain in observation mode')
        if settings.max_charge_w>charge or settings.max_discharge_w>discharge:
            raise ValueError('configured power limit exceeds hardware limit')
    stopped=threading.Event();mutex=threading.Lock();state={'payload':None,'received':0,'connected':False}
    def shutdown(*_):stopped.set()
    signal.signal(signal.SIGINT,shutdown);signal.signal(signal.SIGTERM,shutdown)
    client=mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,client_id=config.get('mqtt_client_id','s10-slave-test'),protocol=mqtt.MQTTv5)
    if config.get('mqtt_user'):client.username_pw_set(config['mqtt_user'],secret('S10_MQTT_PASSWORD'))
    if config.get('mqtt_tls',False):client.tls_set()
    def connect(c,u,flags,reason,properties=None):
        with mutex:state['connected']=not reason.is_failure
        if not reason.is_failure:c.subscribe(config.get('master_topic','e3dcMaster/ha/state'))
    def disconnect(c,u,flags,reason,properties=None):
        with mutex:state['connected']=False;state['payload']=None
    def message(c,u,msg):
        if msg.retain:return
        try:
            payload=json.loads(msg.payload)
            if not isinstance(payload,dict):raise ValueError()
        except (ValueError,UnicodeError):payload=None
        with mutex:state['payload']=payload;state['received']=time.monotonic()
    client.on_connect=connect;client.on_disconnect=disconnect;client.on_message=message
    limits=PowerLimits(s10,settings,execute=a.execute,communication_errors=(SendError,))
    output_started=False
    def send(mode,watts):
        # Tags/types/modes checked against E3DC-Control rscp_client.py and MODE_*.
        req=('EMS_REQ_SET_POWER',RscpType.Container,[('EMS_REQ_SET_POWER_MODE',RscpType.UChar8,mode),('EMS_REQ_SET_POWER_VALUE',RscpType.Int32,int(watts))])
        reply=s10.sendRequest(req,retries=0)
        def error(x):
            if isinstance(x,(list,tuple)):
                if len(x)==3 and getattr(x[1],'name',x[1]) in ('Error',RscpType.Error):return True
                return any(error(v) for v in x)
            return False
        if reply is None or error(reply):raise RuntimeError('RSCP command not accepted')
        # A response is not proof of achieved battery power. Measured power is logged separately.
    try:
        client.connect(config['mqtt_host'],int(config.get('mqtt_port',1883)),keepalive=30);client.loop_start()
        settings=limits.start(time.monotonic())
        controller=Controller(settings)
        print(json.dumps({'event':'power_settings','execute':a.execute,'power_settings':limits.receipt}),flush=True)
        recovering=False; failures=0; recovery_after=0
        while not stopped.is_set():
            attempted_command=None
            try:
                recovery_ready=False
                phase='poll';data=s10.poll(keepAlive=True)
                phase='power_settings_readback';limits.check(time.monotonic(),force=recovering)
                # Erst nach allen möglicherweise wartenden Lesezugriffen die Frische bewerten.
                now=time.time()
                with mutex:master=state['payload'];received=state['received'];connected=state['connected']
                try:
                    if not connected or not master or time.monotonic()-received>settings.max_age_s:raise ValueError()
                    if master.get('available') is not True or master.get('context_valid') is not True:raise ValueError()
                    if master.get('live_sample_valid') is False:raise ValueError()
                    slave_time=data['time'].timestamp()
                    # MQTT ts bezeichnet bislang die Veröffentlichung, nicht sicher die Messung.
                    master_time=float(master['ts'])
                    if max(slave_time,master_time)>now+2:raise ValueError()
                    if recovering and min(slave_time,master_time)<=recovery_after:raise ValueError()
                    r=Reading(min(master_time,slave_time),float(master['free_for_consumers_w']),float(master['pv_w']),float(master['grid_w']),float(master['battery_w']),float(master['battery_soc']),float(data['consumption']['battery']),float(data['stateOfCharge']))
                    d=controller.tick(r,now)
                    if recovering and d.reason!='stale_or_invalid':
                        d=controller.stop(now,'rscp_recovered_neutral')
                        controller.neutral_until=max(controller.neutral_until,now+20)
                        recovery_ready=True
                except (KeyError,TypeError,ValueError,OverflowError):
                    r=None;d=controller.stop(now,'invalid_or_missing_sample')
                command=None
                if a.execute and not stopped.is_set():
                    mode,watts=limits.bound(d.mode,d.watts)
                    attempted_command={'mode':mode,'watts':watts}
                    output_started=True;phase='set_power'
                    send({'idle':1,'charge':4,'discharge':2}[mode],watts)
                    command={'mode':mode,'watts':watts}
                if recovery_ready:
                    recovering=False;failures=0
                    event('rscp_recovered',execute=a.execute,neutral_s=max(20,settings.neutral_s))
                print(json.dumps({'time':datetime.datetime.now().astimezone().isoformat(),'execute':a.execute,'decision':d.__dict__,'command':command,'reading':r.__dict__ if r else None,'power_settings':limits.receipt,'receipt':'issued_unconfirmed' if command else 'observation_only'}),flush=True)
                stopped.wait(settings.interval_s)
            except SendError as exc:
                failures+=1;recovering=True;recovery_after=time.time()
                controller.stop(recovery_after,'rscp_unconfirmed')
                with mutex:state['payload']=None;state['received']=0
                s10.disconnect()
                event('rscp_error',execute=a.execute,phase=phase,error=type(exc).__name__,
                      attempted_command=attempted_command, **error_diagnostics(exc),
                      consecutive_failures=failures,receipt='output_unconfirmed' if a.execute else 'observation_only',
                      retry_in_s=min(30,5*2**(failures-1)))
                if failures>=6:
                    raise
                stopped.wait(min(30,5*2**(failures-1)))
    except Exception as exc:
        event('stopped',execute=a.execute,error=str(exc) if isinstance(exc,PowerSettingsError) else type(exc).__name__,
              power_settings=limits.receipt, **error_diagnostics(exc))
        raise
    finally:
        if a.execute and output_started:
            try:send(1,0)
            except Exception as e:
                event('final_idle_unconfirmed',error=type(e).__name__, **error_diagnostics(e))
                logging.error('Final IDLE unconfirmed: %s',type(e).__name__)
        if a.execute:print(json.dumps({'event':'final_power_settings','last_known':True,'power_settings':limits.receipt}),flush=True)
        client.disconnect();client.loop_stop();s10.disconnect();lock.close()

if __name__=='__main__':
    try:run()
    except PowerSettingsError as e:
        logging.error('Stopped: %s',str(e))
        raise SystemExit(1)
    except Exception as e:
        logging.error('Stopped: %s',type(e).__name__)
        raise SystemExit(1)
