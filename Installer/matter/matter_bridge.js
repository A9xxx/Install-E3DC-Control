import { MatterServer, CommissioningServer } from "@project-chip/matter-node.js";
import { OnOffPluginUnitDevice, DimmableLightDevice } from "@project-chip/matter-node.js/device";
import { BridgedDeviceBasicInformationCluster, ClusterServer } from "@project-chip/matter-node.js/cluster";
import fs from "fs";
import path from "path";
import { spawn, execSync } from "child_process";
import { StorageManager, StorageBackendDisk } from "@project-chip/matter-node.js/storage";
import { loadOrCreateCommissioningCredentials } from "./commissioning_credentials.js";

const PAIRING_FILE = "/var/www/html/ramdisk/matter_pairing.json";
const LIVE_DATA_FILE = "/var/www/html/ramdisk/live_data_py.json";
const WALLBOX_DATA_FILE = "/var/www/html/ramdisk/wallbox_native.json";
const MAX_LIVE_BYTES = 4 * 1024 * 1024;

function generation(stats) {
    return [
        stats.dev,
        stats.ino,
        stats.size,
        stats.mtimeNs,
        stats.ctimeNs,
    ];
}

function sameGeneration(left, right) {
    const leftGeneration = generation(left);
    const rightGeneration = generation(right);
    return leftGeneration.every((value, index) => value === rightGeneration[index]);
}

function freshEnough(stats, maxAgeMs) {
    const nowNs = BigInt(Date.now()) * 1000000n;
    const ageNs = nowNs > stats.mtimeNs ? nowNs - stats.mtimeNs : 0n;
    return ageNs <= BigInt(maxAgeMs) * 1000000n;
}

function readBoundJsonObject(file, maxAgeMs, maxBytes = MAX_LIVE_BYTES) {
    let descriptor;
    try {
        const initial = fs.lstatSync(file, { bigint: true });
        if (
            !initial.isFile()
            || initial.nlink !== 1n
            || initial.size <= 1n
            || initial.size > BigInt(maxBytes)
            || !freshEnough(initial, maxAgeMs)
        ) {
            return null;
        }
        descriptor = fs.openSync(
            file,
            fs.constants.O_RDONLY
            | (fs.constants.O_CLOEXEC ?? 0)
            | (fs.constants.O_NOFOLLOW ?? 0),
        );
        const before = fs.fstatSync(descriptor, { bigint: true });
        if (
            !before.isFile()
            || before.nlink !== 1n
            || before.size <= 1n
            || before.size > BigInt(maxBytes)
            || !freshEnough(before, maxAgeMs)
            || !sameGeneration(initial, before)
        ) {
            return null;
        }
        const source = fs.readFileSync(descriptor);
        const after = fs.fstatSync(descriptor, { bigint: true });
        const current = fs.lstatSync(file, { bigint: true });
        if (
            BigInt(source.length) !== after.size
            || !sameGeneration(before, after)
            || !sameGeneration(after, current)
            || !current.isFile()
            || current.nlink !== 1n
        ) {
            return null;
        }
        const parsed = JSON.parse(source.toString("utf-8").replace(/^\uFEFF/, ""));
        return parsed && typeof parsed === "object" && !Array.isArray(parsed)
            ? parsed
            : null;
    } catch (_) {
        return null;
    } finally {
        if (descriptor !== undefined) {
            try { fs.closeSync(descriptor); } catch (_) {}
        }
    }
}

function finiteNumber(source, keys) {
    for (const key of keys) {
        if (!(key in source) || source[key] === null || source[key] === "") continue;
        const value = Number(source[key]);
        if (Number.isFinite(value)) return value;
    }
    return null;
}

function readMatterLiveSnapshot() {
    const live = readBoundJsonObject(LIVE_DATA_FILE, 15000);
    if (!live) return null;
    const pv = finiteNumber(live, ["PV_Power", "pv"]);
    const grid = finiteNumber(live, ["Grid_Power", "grid"]);
    const battery = finiteNumber(live, ["Battery_Power", "bat"]);
    const home = finiteNumber(live, ["Home_Power", "home_raw", "home"]);
    const soc = finiteNumber(live, ["SOC", "soc"]);
    if (
        [pv, grid, battery, home, soc].some(value => value === null)
        || live.RSCP_Sample_Valid !== true
        || live.Grid_Power_Valid !== true
    ) {
        return null;
    }

    let wallbox = Math.max(
        0,
        finiteNumber(live, ["Wallbox_Power", "wb"]) ?? 0,
    );
    const companion = readBoundJsonObject(WALLBOX_DATA_FILE, 30000);
    if (companion) {
        const detailPower = new Map();
        const details = Array.isArray(companion.wb_details)
            ? companion.wb_details
            : [];
        for (const detail of details) {
            if (!detail || typeof detail !== "object") continue;
            const id = Number(detail.id);
            if (id !== 1 && id !== 2) continue;
            const power = finiteNumber(
                detail,
                ["charge_power_w", "power_w", "real_power_w"],
            );
            if (power !== null) {
                detailPower.set(
                    id,
                    Math.max(detailPower.get(id) ?? 0, Math.max(0, power)),
                );
            }
        }
        const companionTotal = finiteNumber(
            companion,
            ["total_power_w", "power_w"],
        );
        const projectedTotal = [...detailPower.values()]
            .reduce((sum, value) => sum + value, 0);
        wallbox = Math.max(
            wallbox,
            Math.max(0, companionTotal ?? projectedTotal),
        );
    }
    return { pv, grid, bat: battery, home_raw: home, soc, wb: wallbox };
}

function writePairingData(data) {
    const tmp = `${PAIRING_FILE}.${process.pid}.tmp`;
    try {
        fs.writeFileSync(tmp, JSON.stringify(data), { encoding: "utf-8", mode: 0o640 });
        fs.renameSync(tmp, PAIRING_FILE);
        fs.chmodSync(PAIRING_FILE, 0o640);
    } catch (error) {
        try { if (fs.existsSync(tmp)) fs.unlinkSync(tmp); } catch (_) {}
        console.error("Matter-Kopplungsstatus konnte nicht lokal gespeichert werden.");
    }
}

// --- mDNS Proxy via avahi-publish-service ---
let avahiProcess = null;

function startAvahiProxy(discriminator, vendorId, productId) {
    stopAvahiProxy();
    try {
        execSync('command -v avahi-publish-service', { stdio: 'ignore', shell: '/bin/sh' });
        const shortDiscriminator = discriminator >> 8;
        const vpStr = `${vendorId}+${productId}`;
        console.log("[mDNS] Registriere lokalen Matter-Commissioning-Dienst via Avahi.");
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

    const commissioningCredentials = loadOrCreateCommissioningCredentials(storageLocation);

    const storage = new StorageBackendDisk(storageLocation);
    const storageManager = new StorageManager(storage);
    await storageManager.initialize();

    const matterServer = new MatterServer(storageManager);

    const VENDOR_ID  = 0xFFF1;
    const PRODUCT_ID = 0x8001;

    const commissioningServer = new CommissioningServer({
        port: 5540,
        deviceName: "E3DC Hauskraftwerk",
        deviceType: 0x000E, // Matter DeviceType für AGGREGATOR (Bridge)
        passcode: commissioningCredentials.passcode,
        discriminator: commissioningCredentials.discriminator,
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
        console.log("Matter Bridge wartet auf eine lokale Kopplung über die geschützte Weboberfläche.");

        const pairingData = {
            manual: pairingCode.manualPairingCode,
            isCommissioned: false
        };
        writePairingData(pairingData);

        startAvahiProxy(commissioningCredentials.discriminator, VENDOR_ID, PRODUCT_ID);
        const watcher = setInterval(() => {
            if (commissioningServer.isCommissioned()) {
                stopAvahiProxy();
                writePairingData({ isCommissioned: true });
                clearInterval(watcher);
            }
        }, 10000);
    } else {
        console.log("Gerät ist bereits gepairt.");
        writePairingData({ isCommissioned: true });
    }

    // =====================================================================
    // LIVE POLLING — Alle 5 Sekunden E3DC Daten holen & Schalter updaten
    // =====================================================================
    setInterval(() => {
        if (!commissioningServer.isCommissioned()) return;
        const data = readMatterLiveSnapshot();
        if (!data) {
            wallboxDevice.setOnOff(false);
            pvDevice.setOnOff(false);
            gridDevice.setOnOff(false);
            return;
        }

        // Wallbox lädt wenn >50W
        wallboxDevice.setOnOff(data.wb > 50);

        // PV produziert wenn >500W
        pvDevice.setOnOff(data.pv > 500);

        // Einspeisung ins Netz wenn >500W Überschuss
        gridDevice.setOnOff(data.grid < -500);
    }, 5000);

    process.on('SIGINT', () => { stopAvahiProxy(); process.exit(0); });
    process.on('SIGTERM', () => { stopAvahiProxy(); process.exit(0); });
}

main().catch(console.error);
