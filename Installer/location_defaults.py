"""Neutrale Standortvorgaben für die PV-Prognose, ohne Nutzermigration."""

DEFAULT_LATITUDE = 51.16
DEFAULT_LONGITUDE = 10.45

# Fingerabdrücke früherer Vorgabepaare, normiert als fünf Nachkommastellen.
LEGACY_LOCATION_HASHES = frozenset({
    '812b381f1e34702cc946307b602e26de5255bdafdfd2ffe3fb9c79908ea4cdb8',
    '9e57b49b5992da3a3942d3e50487ba3dec38032f14c29e50d941e8a5549d999f',
    'c41d748916e4e9101ced45d8d2c71c76c3b33fa2f6171359c046b70f074433fd',
    'cc8061067d62c00d191b366942e5acf46ebeb7c01806ff3453605c70b1faf06e',
    'd307798ef1b59f5ce354060ceb571c57029432488f19372f6ece57ad1e2d3bed',
})
