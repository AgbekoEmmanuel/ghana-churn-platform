"""
api/profile.py - synthetic KYC / personal details for a customer
=================================================================
The Kaggle data (and so our simulator) has no names, phone numbers, Ghana Card
numbers or dates of birth. A real bank would hold these, so the platform makes
them up - but DETERMINISTICALLY: every detail is derived from the customer_id
with a stable hash, so the same customer always shows the same name, phone and
card on every refresh, on every machine, without storing anything new.

Everything here is clearly SYNTHETIC (the API flags it) and never reaches the
churn or dormancy models.

Real columns are respected where they exist: age, gender (when stored),
region, estimated_salary, salary_credited, products_number, credit_card, tenure.
"""

import hashlib
from datetime import date, datetime, timedelta

# ---------------------------------------------------------------------
# Stable pseudo-randomness: same (customer_id, purpose) -> same number
# ---------------------------------------------------------------------
def _h(customer_id: int, purpose: str) -> int:
    digest = hashlib.blake2b(f"{customer_id}:{purpose}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def _pick(customer_id: int, purpose: str, options):
    return options[_h(customer_id, purpose) % len(options)]


def _chance(customer_id: int, purpose: str, p: float) -> bool:
    return (_h(customer_id, purpose) % 10_000) < p * 10_000


# ---------------------------------------------------------------------
# Gender: stored for historical (Kaggle) customers and for new simulated ones.
# Older simulated rows have none, so we derive one with the same 54.6% male
# share as the Kaggle book. The SQL version below MUST match this function.
# ---------------------------------------------------------------------
GENDER_SQL = """(CASE WHEN gender IS NOT NULL THEN gender
                      WHEN mod(customer_id * 7919, 1000) < 546 THEN 'Male'
                      ELSE 'Female' END)"""


def resolve_gender(customer_id: int, stored) -> str:
    if stored:
        return stored
    return "Male" if (customer_id * 7919) % 1000 < 546 else "Female"


# ---------------------------------------------------------------------
# Name pools (Ghanaian names, grouped by where they are most common)
# ---------------------------------------------------------------------
FIRST = {
    "Male": {
        "akan": ["Kwame", "Kofi", "Kwabena", "Kwaku", "Yaw", "Kwasi", "Kojo", "Kwadwo", "Nana", "Kwesi"],
        "christian": ["Emmanuel", "Daniel", "Samuel", "Isaac", "Prince", "Richard", "Michael", "Joseph",
                      "Ebenezer", "Benjamin", "Eric", "Stephen", "Francis", "George", "Felix", "Bernard"],
        "ewe": ["Selorm", "Edem", "Elikem", "Senyo", "Mawuli", "Delali", "Kafui", "Eyram"],
        "north": ["Abdul", "Ibrahim", "Mohammed", "Fuseini", "Alhassan", "Issah", "Yakubu", "Sumaila"],
        "upper": ["Atinga", "Ayine", "Azure", "Akolbire", "Naab", "Dery", "Bayor", "Ziem"],
    },
    "Female": {
        "akan": ["Ama", "Akosua", "Abena", "Akua", "Yaa", "Afua", "Adwoa", "Esi", "Efua", "Araba"],
        "christian": ["Grace", "Mercy", "Gifty", "Abigail", "Priscilla", "Comfort", "Joyce", "Vida",
                      "Patience", "Esther", "Linda", "Doris", "Rita", "Gloria", "Sandra", "Belinda"],
        "ewe": ["Dzifa", "Enyonam", "Esinam", "Sena", "Mawusi", "Elinam", "Xorlali", "Eyra"],
        "north": ["Amina", "Fatima", "Zainab", "Rahinatu", "Ayishetu", "Mariama", "Hawa", "Salamatu"],
        "upper": ["Atinpoka", "Asibi", "Akanyele", "Aninyi", "Pognaa", "Ayamba", "Awinbe", "Abaa"],
    },
}
SURNAMES = {
    "akan": ["Mensah", "Owusu", "Boateng", "Asante", "Osei", "Appiah", "Agyeman", "Ofori", "Darko",
             "Amoah", "Frimpong", "Antwi", "Acheampong", "Sarpong", "Yeboah", "Opoku", "Bonsu", "Adomako"],
    "ga": ["Quaye", "Tetteh", "Lamptey", "Addo", "Quartey", "Ankrah", "Nortey", "Okine", "Laryea", "Odoi"],
    "fante": ["Arthur", "Quansah", "Ackah", "Essel", "Eshun", "Annan", "Baidoo", "Hagan", "Kwofie", "Blay"],
    "ewe": ["Ahiable", "Dzokoto", "Kpodo", "Amegashie", "Gbedemah", "Adzraku", "Tsikata", "Akpabli",
            "Kumordzi", "Dogbe"],
    "north": ["Abdulai", "Seidu", "Mahama", "Yakubu", "Alhassan", "Iddrisu", "Salifu", "Bawa", "Fuseini"],
    "upper": ["Ayamga", "Akolgo", "Atanga", "Abugri", "Anaba", "Kuuire", "Bayor", "Ziem", "Bagbin"],
}
# Which name traditions are most common in each region (first entry most likely).
REGION_GROUPS = {
    "Greater Accra": ["ga", "akan", "ewe", "fante"],
    "Ashanti": ["akan", "akan", "akan", "north"],
    "Western": ["fante", "akan", "akan", "ewe"],
    "Eastern": ["akan", "akan", "ga", "ewe"],
    "Central": ["fante", "fante", "akan", "ga"],
    "Northern": ["north", "north", "north", "akan"],
    "Volta": ["ewe", "ewe", "ewe", "akan"],
    "Bono": ["akan", "akan", "north", "akan"],
    "Upper East": ["upper", "upper", "north", "akan"],
    "Upper West": ["upper", "upper", "north", "akan"],
}
FIRST_FOR_GROUP = {"ga": "christian", "fante": "akan", "akan": "akan", "ewe": "ewe", "north": "north", "upper": "upper"}

REGION_INFO = {   # towns, neighbourhoods, GhanaPost GPS prefix, common language
    "Greater Accra": (["Accra", "Tema", "Madina", "Adenta", "Teshie", "Dansoman", "Achimota", "East Legon"],
                      ["Spintex Road", "Community 25", "Kokomlemle", "Abelemkpe", "Haatso", "Osu", "Lashibi"],
                      "GA", "Ga / Twi"),
    "Ashanti": (["Kumasi", "Obuasi", "Ejisu", "Konongo", "Mampong", "Bekwai"],
                ["Asokwa", "Bantama", "Suame", "Ahodwo", "Tafo", "Kwadaso", "Santasi"], "AK", "Twi"),
    "Western": (["Takoradi", "Sekondi", "Tarkwa", "Prestea", "Axim"],
                ["Anaji", "Effiakuma", "Kwesimintsim", "Fijai", "Airport Ridge"], "WS", "Fante / Nzema"),
    "Eastern": (["Koforidua", "Nkawkaw", "Akim Oda", "Nsawam", "Suhum"],
                ["Adweso", "Effiduase", "Old Estate", "Zongo", "Nyamekrom"], "EA", "Twi"),
    "Central": (["Cape Coast", "Kasoa", "Winneba", "Mankessim", "Agona Swedru"],
                ["Abura", "Pedu", "Kotokuraba", "Ola", "Amanful"], "CP", "Fante"),
    "Northern": (["Tamale", "Yendi", "Savelugu", "Bimbilla"],
                 ["Lamashegu", "Kalpohin", "Vittin", "Sakasaka", "Kanvili"], "NT", "Dagbani"),
    "Volta": (["Ho", "Hohoe", "Keta", "Aflao", "Kpando"],
              ["Bankoe", "Dome", "Ahoe", "Heve", "Godokpe"], "VR", "Ewe"),
    "Bono": (["Sunyani", "Berekum", "Dormaa Ahenkro", "Wenchi"],
             ["Abesim", "Penkwase", "Odumase", "Nkwabeng"], "BO", "Bono Twi"),
    "Upper East": (["Bolgatanga", "Navrongo", "Bawku", "Zebilla"],
                   ["Zaare", "Soe", "Atulbabisi", "Tindonsobligo"], "UE", "Gurune / Kusaal"),
    "Upper West": (["Wa", "Tumu", "Lawra", "Jirapa"],
                   ["Kambali", "Dobile", "Kpaguri", "Danko"], "UW", "Dagaare / Waale"),
}
NETWORKS = [("MTN", ["024", "054", "055", "059", "053"], 0.62),
            ("Telecel", ["020", "050"], 0.2),
            ("AirtelTigo", ["027", "057", "026", "056"], 0.18)]

OCCUPATIONS = {
    "salaried_high": ["Software engineer", "Medical doctor", "Bank manager", "Lawyer", "Petroleum engineer",
                      "Senior civil servant", "IT consultant", "Pharmacist", "University lecturer"],
    "salaried_mid": ["Teacher", "Nurse", "Accountant", "Civil servant", "Police officer", "Procurement officer",
                     "Radiographer", "Marketing officer", "HR officer", "Customs officer"],
    "salaried_low": ["Driver", "Security officer", "Office clerk", "Sales assistant", "Receptionist",
                     "Warehouse assistant", "Cleaner", "Bank teller"],
    "self_high": ["Importer", "Real estate developer", "Business owner", "Pharmacy owner", "Building contractor"],
    "self_mid": ["Trader", "Fashion designer", "Caterer", "Shop owner", "Hairdresser", "Transport operator",
                 "Mobile money agent", "Event planner"],
    "self_farm": ["Farmer", "Cocoa farmer", "Livestock farmer", "Agro-processor"],
}
SELF_FARM_REGIONS = {"Northern", "Upper East", "Upper West", "Bono", "Ashanti", "Eastern", "Volta"}
EXTRA_PRODUCTS = ["Current account", "Fixed deposit", "Personal loan", "Treasury bill investment", "Mortgage"]


def income_tier(monthly: float) -> str:
    for upper, label in ((1500, "Under GH₵ 1,500"), (3000, "GH₵ 1,500 – 3,000"), (5000, "GH₵ 3,000 – 5,000"),
                         (10000, "GH₵ 5,000 – 10,000"), (15000, "GH₵ 10,000 – 15,000")):
        if monthly < upper:
            return label
    return "GH₵ 15,000+"


def _years_before(d: date, years: int) -> date:
    try:
        return d.replace(year=d.year - years)
    except ValueError:            # 29 February
        return d.replace(year=d.year - years, day=28)


def _age_on(dob: date, today: date) -> int:
    return today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))


def build_profile(c: dict) -> dict:
    """All the personal / KYC details for one customer row (a dict from bank_customers)."""
    cid = int(c["customer_id"])
    region = c.get("region") or "Greater Accra"
    gender = resolve_gender(cid, c.get("gender"))
    age = int(c.get("age") or 35)

    # --- Name: first name and surname follow the region's naming traditions ---
    groups = REGION_GROUPS.get(region, ["akan", "ga", "ewe", "fante"])
    group = groups[_h(cid, "group") % len(groups)]
    if _chance(cid, "migrant", 0.18):                     # people move: some names from elsewhere
        group = _pick(cid, "migrant_group", list(SURNAMES))
    surname = _pick(cid, "surname", SURNAMES[group])
    first_style = FIRST_FOR_GROUP[group] if not _chance(cid, "christian", 0.45) else "christian"
    if group in ("north", "upper") and first_style == "christian" and _chance(cid, "keep_north", 0.6):
        first_style = group
    first = _pick(cid, "first", FIRST[gender][first_style])
    middle = _pick(cid, "middle", FIRST[gender]["akan" if group != "north" else "christian"]) \
        if _chance(cid, "has_middle", 0.55) else None
    if middle == first:
        middle = None
    full_name = " ".join(p for p in (first, middle, surname) if p)

    # --- Date of birth: consistent with the age the bank recorded when they joined the platform ---
    created = c.get("created_at")
    anchor = created.date() if isinstance(created, datetime) else date.today()
    dob = _years_before(anchor, age) - timedelta(days=_h(cid, "dob") % 365)
    today = date.today()

    # --- Contact ---
    r = (_h(cid, "network") % 1000) / 1000
    cum = 0.0
    for net_name, prefixes, share in NETWORKS:
        cum += share
        if r < cum:
            break
    prefix = _pick(cid, "prefix", prefixes)
    digits = f"{_h(cid, 'phone') % 10_000_000:07d}"
    phone = f"+233 {prefix[1:]} {digits[:3]} {digits[3:]}"
    domain = _pick(cid, "mail", ["gmail.com", "gmail.com", "gmail.com", "yahoo.com", "outlook.com"])
    email = f"{first.lower()}.{surname.lower()}{_h(cid, 'mailno') % 90 + 10}@{domain}"

    # --- Ghana Card (National ID): GHA-XXXXXXXXX-X ---
    card_digits = f"{_h(cid, 'card') % 1_000_000_000:09d}"
    check = sum(int(d) * (i + 1) for i, d in enumerate(card_digits)) % 10
    ghana_card = f"GHA-{card_digits}-{check}"

    # --- Address ---
    towns, areas, gps_code, language = REGION_INFO.get(region, REGION_INFO["Greater Accra"])
    town = _pick(cid, "town", towns)
    area = _pick(cid, "area", areas)
    house = _h(cid, "house") % 180 + 1
    gps = f"{gps_code}-{_h(cid, 'gps1') % 900 + 100}-{_h(cid, 'gps2') % 9000 + 1000}"

    # --- Employment & income (income is the Kaggle salary, read as GH₵ per year) ---
    annual = float(c.get("estimated_salary") or 0)
    monthly = annual / 12
    salaried = bool(c.get("salary_credited"))
    if age <= 23 and monthly < 6000 and not salaried:
        status, occupation = "Student", _pick(cid, "occ", ["University student", "Tertiary student", "Apprentice"])
    elif age >= 60 and not salaried:
        status, occupation = "Retired", "Retired (pensioner)"
    elif salaried:
        band = "salaried_high" if monthly >= 10000 else "salaried_mid" if monthly >= 4000 else "salaried_low"
        status, occupation = "Salaried employee", _pick(cid, "occ", OCCUPATIONS[band])
    else:
        if region in SELF_FARM_REGIONS and _chance(cid, "farm", 0.35):
            band = "self_farm"
        else:
            band = "self_high" if monthly >= 12000 else "self_mid"
        status, occupation = "Self-employed", _pick(cid, "occ", OCCUPATIONS[band])

    # --- Marital status & next of kin ---
    p_married = 0.08 if age < 25 else 0.45 if age < 32 else 0.72 if age < 60 else 0.6
    if _chance(cid, "married", p_married):
        marital = "Married"
    elif age >= 60 and _chance(cid, "widowed", 0.35):
        marital = "Widowed"
    elif age >= 35 and _chance(cid, "divorced", 0.1):
        marital = "Divorced"
    else:
        marital = "Single"
    if marital == "Married":
        kin_rel = "Spouse"
        kin_gender = "Female" if gender == "Male" else "Male"
        kin_surname = surname
    else:
        kin_rel = "Parent" if age < 35 else ("Child" if age >= 55 else "Sibling")
        kin_gender = "Female" if _chance(cid, "kin_g", 0.55) else "Male"
        kin_surname = surname if kin_rel != "Child" or _chance(cid, "kin_s", 0.7) else _pick(cid, "kin_s2", SURNAMES[group])
    kin_first = _pick(cid, "kin_first", FIRST[kin_gender]["christian" if _chance(cid, "kin_c", 0.5) else FIRST_FOR_GROUP[group]])
    kin_digits = f"{_h(cid, 'kin_phone') % 10_000_000:07d}"
    kin_prefix = _pick(cid, "kin_prefix", ["024", "054", "055", "020", "027", "059"])

    # --- Accounts & products ---
    branch_code = _h(cid, "branch") % 90 + 10
    acct = f"{branch_code:02d}{_h(cid, 'acct') % 10**11:011d}"
    products = ["Savings account"]
    extras = sorted(EXTRA_PRODUCTS, key=lambda p: _h(cid, "prod:" + p))
    products += extras[: max(0, int(c.get("products_number") or 1) - 1)]
    if salaried and "Current account" not in products:
        products[0] = "Salary account"
    if c.get("credit_card"):
        products.append("Credit card")

    return {
        "synthetic": True,
        "full_name": full_name,
        "first_name": first,
        "surname": surname,
        "initials": (first[0] + surname[0]).upper(),
        "gender": gender,
        "date_of_birth": dob.isoformat(),
        "age": _age_on(dob, today),
        "marital_status": marital,
        "ghana_card": ghana_card,
        "phone": phone,
        "phone_network": net_name,
        "email": email,
        "preferred_language": language,
        "address": f"House {house}, {area}, {town}",
        "digital_address": gps,
        "town": town,
        "home_branch": f"{town} Branch",
        "employment_status": status,
        "occupation": occupation,
        "monthly_income": round(monthly, 2),
        "income_tier": income_tier(monthly),
        "account_number": f"{acct[:4]} {acct[4:8]} {acct[8:]}",
        "account_type": products[0],
        "products": products,
        "kyc_status": "Re-verification due" if _chance(cid, "kyc", 0.08) else "Verified",
        "next_of_kin": {
            "name": f"{kin_first} {kin_surname}",
            "relationship": kin_rel,
            "phone": f"+233 {kin_prefix[1:]} {kin_digits[:3]} {kin_digits[3:]}",
        },
    }


def list_identity(c: dict) -> dict:
    """The light version used in table rows: name, gender, initials."""
    p = build_profile(c)
    return {"full_name": p["full_name"], "initials": p["initials"], "gender": p["gender"],
            "phone": p["phone"]}
