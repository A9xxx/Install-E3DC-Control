import { MatterServer, CommissioningServer } from "@project-chip/matter-node.js";
import { OnOffPluginUnitDevice, DimmableLightDevice } from "@project-chip/matter-node.js/device";
import { BridgedDeviceBasicInformationCluster, ClusterServer } from "@project-chip/matter-node.js/cluster";
import fs from "fs";
import path from "path";
import http from "http";
import { spawn, execSync } from "child_process";
import { StorageManager, StorageBackendDisk } from "@project-chip/matter-node.js/storage";

const PAIRING_FILE = "/var/www/html/ramdisk/matter_pairing.json";

// --- mDNS Proxy via avahi-publish-service ---
let avahiProcess = null;

function startAvahiProxy(discriminator, vendorId, productId) {
    stopAvahiProxy();
    try {
        try { execSync('systemctl is-active avahi-daemon', { stdio: 'ignore' }); }
        catch {
            try { execSync('systemctl start avahi-daemon', { stdio: 'ignore' }); }
            catch(e) { console.log("Avahi-Daemon konnte nicht gestartet werden:", e.message); return; }
        }
        const shortDiscriminator = discriminator >> 8;
        const vpStr = `${vendorId}+${productId}`;
        console.log(`[mDNS] Registriere _matterc._udp via avahi (D=${discriminator}, VP=${vpStr})`);
        avahiProcess = spawn('avahi-publish-service', [
            '--subtype', `_L${discriminator}._sub._matterc._udp`,
            '--subtype', `_S${shortDiscriminator}._sub._matterc._udp`,
            'E3DC-Hauskraftwerk', '_matterc._udp', '5540',
            `D=${discriminator}`, `VP=${vpStr}`, 'CM=1', 'PH=36', 'AP=0', 'T=1'
        ]);
        avahiProcess.on('exit', () => { avahiProcess = null; });
    } catch(e) {
        console.log('[mDNS] avahi-publish-service nicht verfügbar:', e.message);
    }
}

function stopAvahiProxy() {
    if (avahiProcess) { try { avahiProcess.kill(); } catch {} avahiProcess = null; }
}

async function main() {
    console.log("Starte E3DC Matter Bridge...");

    let storageLocation = path.join(process.cwd(), "matter-storage");
    if (fs.existsSync("/var/www/html/data")) storageLocation = "/var/www/html/data/matter-storage";

    const storage = new StorageBackendDisk(storageLocation);
    const storageManager = new StorageManager(storage);
    await storageManager.initialize();

    const matterServer = new MatterServer(storageManager);

    const DISCRIMINATOR = 3840;
    const VENDOR_ID  = 0xFFF1;
    const PRODUCT_ID = 0x8001;
    const PASSCODE   = 20202021;

    const commissioningServer = new CommissioningServer({
        port: 5540,
        deviceName: "E3DC Hauskraftwerk",
        deviceType: 0x000E, // Matter DeviceType für AGGREGATOR (Bridge)
        passcode: PASSCODE,
        discriminator: DISCRIMINATOR,
        basicInformation: {
            vendorName: "A9x",
            vendorId: VENDOR_ID,
            productId: PRODUCT_ID,
            nodeLabel: "E3DC-Control",
            productName: "E3DC Matter Bridge",
            productLabel: "E3DC Matter Bridge",
            serialNumber: "E3DC-BRIDGE-1",
        }
    });

    // =====================================================================
    // 3 LIVE-SCHALTER — Automations-Trigger für Google Home & Apple Home
    // =====================================================================

    // Schalter 1: Wallbox — An wenn E-Auto lädt (>50W)
    function setBridgeDeviceInfo(device, label, serialSuffix) {
        device.addClusterServer(ClusterServer(
            BridgedDeviceBasicInformationCluster,
            {
                vendorName: "A9x",
                vendorId: VENDOR_ID,
                productName: label,
                productLabel: label,
                nodeLabel: label,
                serialNumber: `E3DC-${serialSuffix}`,
                uniqueId: `e3dc-${serialSuffix.toLowerCase()}`,
                reachable: true,
                softwareVersion: 1,
                softwareVersionString: "v1",
                hardwareVersion: 1,
                hardwareVersionString: "v1",
            },
            {},
            {
                reachableChanged: true,
            }
        ));
        return device;
    }

    function makeSwitch(label, serialSuffix) {
        return setBridgeDeviceInfo(new OnOffPluginUnitDevice(undefined, {
            uniqueStorageKey: serialSuffix.toLowerCase(),
        }), label, serialSuffix);
    }

    const wallboxDevice = makeSwitch("E3DC Wallbox aktiv", "WALLBOX-AKTIV");
    commissioningServer.addDevice(wallboxDevice);

    // Schalter 2: PV-Produktion — An wenn Sonne >500W produziert
    const pvDevice = makeSwitch("E3DC PV produziert", "PV-PRODUZIERT");
    commissioningServer.addDevice(pvDevice);

    // Schalter 3: Netz-Einspeisung — An wenn Strom-Überschuss ins Netz fließt (>500W)
    const gridDevice = makeSwitch("E3DC Einspeisung aktiv", "EINSPEISUNG-AKTIV");
    commissioningServer.addDevice(gridDevice);

    // Bridge und Endpunkte beim Matter-Server registrieren.
    matterServer.addCommissioningServer(commissioningServer);
    await matterServer.start();

    const isCommissioned = commissioningServer.isCommissioned();
    if (!isCommissioned) {
        const pairingCode = commissioningServer.getPairingCode();
        console.log("Manual Pairing Code:", pairingCode.manualPairingCode);
        console.log("QR Code:", pairingCode.qrPairingCode);

        const pairingData = {
            manual: pairingCode.manualPairingCode,
            qrText: pairingCode.qrPairingCode,
            isCommissioned: false
        };
        try { fs.writeFileSync(PAIRING_FILE, JSON.stringify(pairingData), 'utf-8'); } catch(e) {}

        startAvahiProxy(DISCRIMINATOR, VENDOR_ID, PRODUCT_ID);
        const watcher = setInterval(() => {
            if (commissioningServer.isCommissioned()) {
                stopAvahiProxy();
                try { fs.writeFileSync(PAIRING_FILE, JSON.stringify({ isCommissioned: true }), 'utf-8'); } catch {}
                clearInterval(watcher);
            }
        }, 10000);
    } else {
        console.log("Gerät ist bereits gepairt.");
        try { fs.writeFileSync(PAIRING_FILE, JSON.stringify({ isCommissioned: true }), 'utf-8'); } catch {}
    }

    // =====================================================================
    // LIVE POLLING — Alle 5 Sekunden E3DC Daten holen & Schalter updaten
    // =====================================================================
    setInterval(() => {
        http.get('http://127.0.0.1/get_live_json.php', (res) => {
            let body = '';
            res.on('data', chunk => body += chunk);
            res.on('end', () => {
                try {
                    const data = JSON.parse(body);
                    if (!commissioningServer.isCommissioned()) return;

                    const wb   = parseFloat(data.wb)   || 0;
                    const pv   = parseFloat(data.pv)   || 0;
                    const grid = parseFloat(data.grid) || 0;

                    // Wallbox lädt wenn >50W
                    wallboxDevice.setOnOff(wb > 50);

                    // PV produziert wenn >500W
                    pvDevice.setOnOff(pv > 500);

                    // Einspeisung ins Netz wenn >500W Überschuss
                    gridDevice.setOnOff(grid < -500);

                } catch(e) { /* Polling-Fehler ignorieren */ }
            });
        }).on('error', () => {});
    }, 5000);

    process.on('SIGINT', () => { stopAvahiProxy(); process.exit(0); });
    process.on('SIGTERM', () => { stopAvahiProxy(); process.exit(0); });
}

main().catch(console.error);
