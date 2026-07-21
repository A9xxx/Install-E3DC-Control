import crypto from "crypto";
import fs from "fs";
import path from "path";

const SCHEMA_VERSION = 1;
const FILE_NAME = "commissioning-credentials.json";
const INVALID_PASSCODES = new Set([
    0,
    11111111,
    22222222,
    33333333,
    44444444,
    55555555,
    66666666,
    77777777,
    88888888,
    99999999,
    12345678,
    87654321,
]);

function isValidPasscode(value) {
    return Number.isInteger(value)
        && value >= 1
        && value <= 99999998
        && !INVALID_PASSCODES.has(value);
}

function isValidDiscriminator(value) {
    return Number.isInteger(value) && value >= 0 && value <= 4095;
}

function validateRecord(record) {
    if (!record || typeof record !== "object") {
        throw new Error("Matter-Commissioning-Daten sind ungültig.");
    }
    if (record.schemaVersion !== SCHEMA_VERSION
        || !isValidPasscode(record.passcode)
        || !isValidDiscriminator(record.discriminator)) {
        throw new Error("Matter-Commissioning-Daten haben ein ungültiges Schema.");
    }
    return {
        passcode: record.passcode,
        discriminator: record.discriminator,
    };
}

function assertPrivateRegularFile(filePath) {
    const stat = fs.lstatSync(filePath);
    if (!stat.isFile() || stat.isSymbolicLink() || stat.nlink !== 1) {
        throw new Error("Matter-Commissioning-Datei ist nicht vertrauenswürdig.");
    }
    if (typeof process.geteuid === "function" && stat.uid !== process.geteuid()) {
        throw new Error("Matter-Commissioning-Datei hat einen unerwarteten Besitzer.");
    }
    fs.chmodSync(filePath, 0o600);
}

function loadExisting(filePath) {
    assertPrivateRegularFile(filePath);
    const raw = fs.readFileSync(filePath, { encoding: "utf-8", flag: "r" });
    return validateRecord(JSON.parse(raw));
}

function generateRecord() {
    let passcode = 0;
    while (!isValidPasscode(passcode)) {
        passcode = crypto.randomInt(1, 99999999);
    }
    return {
        schemaVersion: SCHEMA_VERSION,
        passcode,
        discriminator: crypto.randomInt(0, 4096),
    };
}

export function loadOrCreateCommissioningCredentials(storageLocation) {
    const root = path.resolve(storageLocation);
    fs.mkdirSync(root, { recursive: true, mode: 0o700 });
    const rootStat = fs.lstatSync(root);
    if (!rootStat.isDirectory() || rootStat.isSymbolicLink() || fs.realpathSync(root) !== root) {
        throw new Error("Matter-Storage ist nicht vertrauenswürdig.");
    }
    fs.chmodSync(root, 0o700);

    const filePath = path.join(root, FILE_NAME);
    if (fs.existsSync(filePath)) {
        return loadExisting(filePath);
    }

    const record = generateRecord();
    const tmpPath = path.join(
        root,
        `.${FILE_NAME}.${process.pid}.${crypto.randomBytes(8).toString("hex")}.tmp`,
    );
    let descriptor;
    try {
        descriptor = fs.openSync(
            tmpPath,
            fs.constants.O_CREAT
                | fs.constants.O_EXCL
                | fs.constants.O_WRONLY
                | (fs.constants.O_NOFOLLOW || 0),
            0o600,
        );
        fs.writeFileSync(descriptor, `${JSON.stringify(record)}\n`, { encoding: "utf-8" });
        fs.fsyncSync(descriptor);
        fs.closeSync(descriptor);
        descriptor = undefined;
        fs.linkSync(tmpPath, filePath);
        fs.unlinkSync(tmpPath);
        assertPrivateRegularFile(filePath);
        return validateRecord(record);
    } catch (error) {
        if (descriptor !== undefined) {
            try { fs.closeSync(descriptor); } catch (_) {}
        }
        try { fs.unlinkSync(tmpPath); } catch (_) {}
        if (error && error.code === "EEXIST" && fs.existsSync(filePath)) {
            return loadExisting(filePath);
        }
        throw error;
    }
}

export const commissioningCredentialFileName = FILE_NAME;
