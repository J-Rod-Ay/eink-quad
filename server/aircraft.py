"""ICAO type designator -> a name a person would actually say.

"B789" means nothing to anyone outside the hobby; "Boeing 787" means something
to everybody, and "Dreamliner" means something to people who have flown on one.
Exact codes first, then a prefix fallback so an unlisted variant still lands on
the right family rather than showing raw.
"""

EXACT = {
    # Boeing
    "B738": "Boeing 737-800", "B38M": "Boeing 737 MAX 8",
    "B739": "Boeing 737-900", "B39M": "Boeing 737 MAX 9",
    "B737": "Boeing 737", "B752": "Boeing 757", "B753": "Boeing 757",
    "B763": "Boeing 767", "B764": "Boeing 767",
    "B772": "Boeing 777", "B77W": "Boeing 777-300ER", "B77L": "Boeing 777",
    "B788": "Boeing 787 Dreamliner", "B789": "Boeing 787 Dreamliner",
    "B78X": "Boeing 787 Dreamliner",
    "B744": "Boeing 747", "B748": "Boeing 747-8",
    # Airbus
    "A319": "Airbus A319", "A320": "Airbus A320", "A321": "Airbus A321",
    "A20N": "Airbus A320neo", "A21N": "Airbus A321neo",
    "A332": "Airbus A330", "A333": "Airbus A330", "A339": "Airbus A330neo",
    "A343": "Airbus A340", "A346": "Airbus A340",
    "A359": "Airbus A350", "A35K": "Airbus A350",
    "A388": "Airbus A380",
    # Regional
    "E75L": "Embraer 175", "E75S": "Embraer 175", "E170": "Embraer 170",
    "E190": "Embraer 190", "E195": "Embraer 195", "E290": "Embraer E2",
    "CRJ2": "Bombardier CRJ", "CRJ7": "Bombardier CRJ",
    "CRJ9": "Bombardier CRJ", "CRJX": "Bombardier CRJ",
    "DH8D": "Dash 8", "AT76": "ATR 72", "AT75": "ATR 72",
    "BCS1": "Airbus A220", "BCS3": "Airbus A220",
    # Business jets. Worth covering properly rather than treating as exotica:
    # on live Tampa data these are about half of everything overhead, and they
    # get featured often because they fly filed routes.
    "C700": "Citation Longitude", "C750": "Citation X",
    "C550": "Cessna Citation", "C560": "Cessna Citation",
    "C56X": "Cessna Citation", "C510": "Citation Mustang",
    "C25A": "Citation CJ2", "C25B": "Citation CJ3", "C25C": "Citation CJ4",
    "C680": "Citation Sovereign", "C68A": "Citation Latitude",
    "LJ35": "Learjet 35", "LJ45": "Learjet 45", "LJ60": "Learjet 60",
    "LJ75": "Learjet 75",
    "H25B": "Hawker 800", "GLF4": "Gulfstream IV", "GLF5": "Gulfstream V",
    "GLF6": "Gulfstream G650", "G280": "Gulfstream G280",
    "GLEX": "Bombardier Global", "GL5T": "Global 5000",
    "CL30": "Challenger 300", "CL35": "Challenger 350", "CL60": "Challenger",
    "F2TH": "Falcon 2000", "FA50": "Falcon 50", "FA7X": "Falcon 7X",
    "E55P": "Phenom 300", "E50P": "Phenom 100",
    # Turboprops and pistons
    "PC12": "Pilatus PC-12", "PC24": "Pilatus PC-24",
    "TBM7": "Daher TBM", "TBM8": "Daher TBM", "TBM9": "Daher TBM",
    "BE20": "King Air", "BE9L": "King Air", "B350": "King Air 350",
    "BE40": "Beechjet", "BE58": "Beechcraft Baron", "BE36": "Beechcraft Bonanza",
    "C172": "Cessna 172", "C182": "Cessna 182", "C206": "Cessna 206",
    "C208": "Cessna Caravan",
    "P28A": "Piper Cherokee", "P28R": "Piper Arrow",
    "P32R": "Piper Saratoga", "PA46": "Piper Malibu",
    "M600": "Piper M600", "M700": "Piper M700",
    "SR20": "Cirrus SR20", "SR22": "Cirrus SR22", "SF50": "Cirrus Vision Jet",
    "DA40": "Diamond DA40", "DA42": "Diamond DA42",
    "L39": "Aero L-39 jet", "C30J": "C-130 Hercules", "C130": "C-130 Hercules",
    "P46T": "Piper Meridian", "RV12": "Van's RV-12", "RV7": "Van's RV-7",
    # Helicopters
    "R44": "Robinson R44", "R66": "Robinson R66", "B06": "Bell JetRanger",
    "AS50": "helicopter", "EC30": "helicopter", "EC35": "helicopter",
    "S76": "helicopter", "H60": "helicopter",
}

# Checked longest-first, so "B78" wins over "B7" and "C25" beats "C2".
PREFIX = {
    "B78": "Boeing 787", "B77": "Boeing 777", "B76": "Boeing 767",
    "B75": "Boeing 757", "B74": "Boeing 747", "B73": "Boeing 737",
    "A38": "Airbus A380", "A35": "Airbus A350", "A34": "Airbus A340",
    "A33": "Airbus A330", "A32": "Airbus A320", "A31": "Airbus A319",
    "CRJ": "Bombardier CRJ", "E19": "Embraer 190", "E17": "Embraer 175",
    "E13": "Embraer regional jet", "E14": "Embraer regional jet",
    "GLF": "Gulfstream", "GLE": "Bombardier Global", "GL5": "Global 5000",
    "C25": "Cessna Citation", "C56": "Cessna Citation",
    "C68": "Cessna Citation", "C75": "Cessna Citation",
    "TBM": "Daher TBM", "P28": "Piper Cherokee", "P32": "Piper Saratoga",
    "PA4": "Piper Malibu", "P46": "Piper Malibu", "RV": "Van's RV", "B35": "King Air", "DA4": "Diamond",
    "EC1": "helicopter", "EC3": "helicopter", "AS3": "helicopter",
    "AS5": "helicopter", "H25": "Hawker",
    "CL": "Challenger", "LJ": "Learjet", "BE": "Beechcraft",
    "SR": "Cirrus", "R4": "Robinson", "R6": "Robinson",
    "FA": "Dassault Falcon", "F2": "Dassault Falcon", "PC": "Pilatus",
    "C1": "Cessna", "C2": "Cessna", "M6": "Piper", "M7": "Piper",
}


def friendly(type_code: str, desc: str = "") -> str:
    """desc would be adsb.lol's own description ("BOEING 737-800"), but on live
    data that field is always null -- so in practice this table is the only
    thing standing between a type code and a blank line. Kept for other feeds.
    Callers must handle "" (render.py falls back to ground speed)."""
    t = (type_code or "").strip().upper()
    if t in EXACT:
        return EXACT[t]
    for n in (3, 2):
        if t[:n] in PREFIX:
            return PREFIX[t[:n]]
    if desc:
        return " ".join(w.capitalize() for w in desc.split())
    return ""
