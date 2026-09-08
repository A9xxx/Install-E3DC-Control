#!/usr/bin/env python3
"""Test runner: observes by default. --execute explicitly enables slave commands."""
import argparse,datetime,json,logging,os,signal,threading,time
from pathlib import Path
from slave_policy import Controller,Settings,Reading
from power_limits import PowerLimits,PowerSettingsError

def run():
    ap=argparse.ArgumentParser();ap.add_argument('--config',required=True);ap.add_argument('--execute',action='store_true');a=ap.parse_args()
    config=json.loads(Path(a.config).read_text(encoding='utf-8-sig'));settings=Settings(**config.get('controller',{}))
    logging.basicConfig(level=logging.INFO,format='%(message)s')
    # Linux process lock, held for the full lifetime; the original script must be stopped separately.
    import fcntl
    lock=open(str(Path(a.config).resolve())+'.lock','a');fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    from e3dc import E3DC
    from e3dc._rscpTags import RscpType
    import paho.mqtt.client as mqtt
    def secret(name):
        value=os.environ.get(name)
        if not value:raise ValueError('required environment variable missing: '+name)
        return value
    class SingleAttemptE3DC(E3DC):
        def sendRequest(self, request, retries=0, keepAlive=False):
            # An uncertain SET reply must not trigger hidden library retries.
            return super().sendRequest(request, retries=0, keepAlive=keepAlive)
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
    limits=PowerLimits(s10,settings,execute=a.execute)
    output_started=False
    def send(mode,watts):
        # Tags/types/modes checked against E3DC-Control rscp_client.py and MODE_*.
        req=('EMS_REQ_SET_POWER',RscpType.Container,[('EMS_REQ_SET_POWER_MODE',RscpType.UChar8,mode),('EMS_REQ_SET_POWER_VALUE',RscpType.Int32,int(watts))])
        reply=s10.sendRequest(req,retries=1)
        def error(x):
            if isinstance(x,(list,tuple)):
                if len(x)==3 and x[1]==RscpType.Error:return True
                return any(error(v) for v in x)
            return False
        if reply is None or error(reply):raise RuntimeError('RSCP command not accepted')
        # A response is not proof of achieved battery power. Measured power is logged separately.
    try:
        client.connect(config['mqtt_host'],int(config.get('mqtt_port',1883)),keepalive=30);client.loop_start()
        settings=limits.start(time.monotonic())
        controller=Controller(settings)
        print(json.dumps({'event':'power_settings','execute':a.execute,'power_settings':limits.receipt}),flush=True)
        while not stopped.is_set():
            data=s10.poll();now=time.time()
            limits.check(time.monotonic())
            with mutex:master=state['payload'];received=state['received'];connected=state['connected']
            try:
                if not connected or not master or time.monotonic()-received>settings.max_age_s:raise ValueError()
                if master.get('available') is not True or master.get('context_valid') is not True:raise ValueError()
                if master.get('live_sample_valid') is False:raise ValueError()
                slave_time=data['time'].timestamp()
                # MQTT ts is the publication time in existing E3DC-Control versions.
                # Upstream measurement freshness is not fully represented by this field.
                master_time=float(master['ts'])
                if max(slave_time,master_time)>now+2:raise ValueError()
                r=Reading(min(master_time,slave_time),float(master['free_for_consumers_w']),float(master['pv_w']),float(master['grid_w']),float(master['battery_w']),float(master['battery_soc']),float(data['consumption']['battery']),float(data['stateOfCharge']))
                d=controller.tick(r,now)
            except (KeyError,TypeError,ValueError,OverflowError):
                r=None;d=controller.stop(now,'invalid_or_missing_sample')
            command=None
            if a.execute:
                mode,watts=limits.bound(d.mode,d.watts)
                output_started=True
                send({'idle':1,'charge':4,'discharge':2}[mode],watts)
                command={'mode':mode,'watts':watts}
            print(json.dumps({'time':datetime.datetime.now().astimezone().isoformat(),'execute':a.execute,'decision':d.__dict__,'command':command,'reading':r.__dict__ if r else None,'power_settings':limits.receipt,'receipt':'issued_unconfirmed' if a.execute else 'observation_only'}),flush=True)
            stopped.wait(settings.interval_s)
    finally:
        if a.execute and output_started:
            try:send(1,0)
            except Exception as e:logging.error('Final IDLE unconfirmed: %s',type(e).__name__)
        if a.execute:print(json.dumps({'event':'final_power_settings','power_settings':limits.receipt}),flush=True)
        client.disconnect();client.loop_stop();s10.disconnect();lock.close()

if __name__=='__main__':
    try:run()
    except PowerSettingsError as e:
        logging.error('Stopped: %s',str(e))
        raise SystemExit(1)
    except Exception as e:
        logging.error('Stopped: %s',type(e).__name__)
        raise SystemExit(1)
