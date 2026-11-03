"""The collected corpus organised by dialect family.

Families, not vendors, carry dialect rules: CODESYS OEMs (WAGO, Schneider
Machine Expert, Bosch ctrlX, Festo examples) share the CODESYS dialect. The
priority letters follow the intake scope: A (CODESYS family), B (Siemens),
C (IEC reference implementations), D (best effort). Vendors without open ST
libraries are listed so the compatibility matrix states why they are absent.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORPUS = ROOT / "rusty_compatibility"


@dataclass(frozen=True)
class Vendor:
    directory: str
    family: str
    priority: str
    dialect: str | None
    note: str = ""


FAMILIES = {
    "codesys": "CODESYS family (CODESYS 2.3/3, TwinCAT 2/3 and CODESYS-based OEM tools)",
    "siemens-scl": "Siemens SCL (TIA Portal and STEP 7 Classic)",
    "simatic-ax": "Siemens SIMATIC AX (ST with namespaces and classes)",
    "iec": "IEC 61131-3 reference implementations (MATIEC/Beremiz, OpenPLC, STruCpp)",
    "sysmac": "Omron Sysmac Studio and KEYENCE KV (JiecLib generated)",
    "gxworks": "Mitsubishi GX Works",
    "br": "B&R Automation Studio",
    "pcworx": "Phoenix Contact PC WORX / MULTIPROG",
    "plcnext": "Phoenix Contact PLCnext Engineer",
    "control-expert": "Schneider Control Expert (Unity Pro)",
    "logix": "Rockwell Logix 5000",
    "none": "No open ST library collected",
}

VENDORS = [
    Vendor("01_CODESYS3", "codesys", "A", "codesys"),
    Vendor("02_CODESYS23", "codesys", "A", "codesys"),
    Vendor("03_TwinCAT3", "codesys", "A", "codesys"),
    Vendor("04_TwinCAT2", "codesys", "A", "codesys"),
    Vendor("13_WAGO", "codesys", "A", "codesys", "CODESYS 3 based"),
    Vendor("09_Schneider_Machine_Expert", "codesys", "A", "codesys", "CODESYS 3 based"),
    Vendor("14_Bosch_ctrlX", "codesys", "A", "codesys", "CODESYS 3 based"),
    Vendor("26_Festo", "codesys", "A", "codesys", "third-party CODESYS examples, not an official Festo library"),
    Vendor("05_Siemens_TIA_SCL", "siemens-scl", "B", "siemens-scl"),
    Vendor("06_Siemens_Classic_SCL", "siemens-scl", "B", "siemens-scl"),
    Vendor("30_Siemens_SIMATIC_AX", "simatic-ax", "B", "simatic-ax"),
    Vendor("32_Beremiz_MATIEC", "iec", "C", "iec"),
    Vendor("33_OpenPLC_v3", "iec", "C", "iec"),
    Vendor("34_OpenPLC_v4_STruCpp", "iec", "C", "iec"),
    Vendor("18_Omron_Sysmac", "sysmac", "D", "sysmac"),
    Vendor("20_KEYENCE_KV", "sysmac", "D", "sysmac", "same JiecLib sources as Sysmac, KEYENCE target"),
    Vendor("19_Mitsubishi_GXWorks", "gxworks", "D", "gxworks"),
    Vendor("08_BR_Automation_Studio", "br", "D", "br"),
    Vendor("16_Phoenix_PC_Worx", "pcworx", "D", "pcworx"),
    Vendor("15_Phoenix_PLCnext", "plcnext", "D", "plcnext"),
    Vendor("10_Schneider_Control_Expert", "control-expert", "D", "control-expert"),
    Vendor("07_Rockwell_Logix", "logix", "D", None,
           "unsupported: different program model (AOIs, global tags, timer structures with .PRE/.ACC/.DN) "
           "and too little open corpus; onboarding would require a translator, not simple substitution"),
]

EXCLUDED = [
    ("11_Schneider_Machine_Expert_Safety", "no open ST library; dialect covered by the CODESYS family"),
    ("12_ABB_AC500", "no open ST library; dialect covered by the CODESYS family"),
    ("17_Omron_CX", "no open ST library"),
    ("21_Panasonic_FPWIN", "no open ST library"),
    ("22_Delta_ISPSoft", "no open ST library; dialect covered by the CODESYS family"),
    ("23_Inovance_InoProShop", "no open ST library; dialect covered by the CODESYS family"),
    ("24_INVT_AX", "no open ST library; dialect covered by the CODESYS family"),
    ("25_Hollysys_LE", "no open ST library; dialect covered by the CODESYS family"),
    ("27_Eaton", "no open ST library; dialect covered by the CODESYS family"),
    ("28_ifm", "no open ST library; dialect covered by the CODESYS family"),
    ("29_Emerson_PACSystems", "no open ST library"),
    ("31_LS_XG5000", "no open ST library"),
]

BY_DIRECTORY = {vendor.directory: vendor for vendor in VENDORS}


def libraries(outputs: Path, vendors: list[Vendor] | None = None) -> list[tuple[Vendor, Path]]:
    """All extracted library files below ``outputs/<vendor>/.st文件``."""
    result = []
    for vendor in vendors or VENDORS:
        for path in sorted((outputs / vendor.directory / ".st文件").glob("*.st")):
            result.append((vendor, path))
    return result
