"""
Εφαρμογή συμπλήρωσης υπεύθυνων δηλώσεων από templates.

Βάζεις .docx templates (με placeholders τύπου {{ onomateponymo }}) στο
doc_templates/. Η εφαρμογή τα διαβάζει, βρίσκει μόνη της τα πεδία που
χρειάζονται τιμή, σου δείχνει μια φόρμα, και σου δίνει έτοιμο .docx με τα
στοιχεία συμπληρωμένα. Δεν αποθηκεύει τίποτα ανάμεσα σε χρήσεις — κάθε φορά
ξεκινάς από άδεια φόρμα.
"""
from __future__ import annotations

import io
import re
import unicodedata
import zipfile
from datetime import date
from pathlib import Path
from typing import Optional

from docxtpl import DocxTemplate
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent
DOC_TEMPLATES_DIR = ROOT / "doc_templates"
UI_DIR = ROOT / "templates"

app = FastAPI(title="Υπεύθυνες Δηλώσεις")

# Ελληνικοί μήνες, για να μπορεί ένα template να γράφει "31 Αυγούστου 2026"
# αν έχει ένα πεδίο σαν {{ imerominia_full }} -- προαιρετική βοήθεια.
GREEK_MONTHS = [
    "Ιανουαρίου", "Φεβρουαρίου", "Μαρτίου", "Απριλίου", "Μαΐου", "Ιουνίου",
    "Ιουλίου", "Αυγούστου", "Σεπτεμβρίου", "Οκτωβρίου", "Νοεμβρίου", "Δεκεμβρίου",
]


def _greek_upper_no_accents(s: str) -> str:
    """Σωστά ελληνικά κεφαλαία: το .upper() της Python ΚΡΑΤΑΕΙ τον τόνο (π.χ.
    'δώδεκα' -> 'ΔΏΔΕΚΑ'), αλλά η σωστή τυπογραφική σύμβαση για ολόκληρες
    λέξεις σε κεφαλαία στα ελληνικά είναι ΧΩΡΙΣ τόνους ('ΔΩΔΕΚΑ'). Αφαιρούμε
    τους τόνους (combining diacritics) μετά το upper() μέσω NFKD."""
    upper = s.upper()
    decomposed = unicodedata.normalize("NFKD", upper)
    without_accents = "".join(c for c in decomposed if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", without_accents)


def _today_greek() -> str:
    d = date.today()
    return f"{d.day} {GREEK_MONTHS[d.month - 1]} {d.year}"


def _iso_to_greek(iso: str) -> str:
    """'2026-08-31' -> '31 Αυγούστου 2026'. Αν δεν είναι έγκυρη ημερομηνία ISO
    (π.χ. άδειο ή ήδη ελεύθερο κείμενο), το αφήνουμε όπως είναι."""
    try:
        d = date.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso
    return f"{d.day} {GREEK_MONTHS[d.month - 1]} {d.year}"


# --- Αριθμός -> ελληνικά λέξεις (μέχρι 9999, αρκεί για μέρα/έτος ημερομηνίας) ---
_UNITS = ["", "ένα", "δύο", "τρία", "τέσσερα", "πέντε", "έξι", "επτά", "οκτώ", "εννέα"]
_TEENS = {
    10: "δέκα", 11: "έντεκα", 12: "δώδεκα", 13: "δεκατρία", 14: "δεκατέσσερα",
    15: "δεκαπέντε", 16: "δεκαέξι", 17: "δεκαεπτά", 18: "δεκαοκτώ", 19: "δεκαεννέα",
}
_TENS = {
    2: "είκοσι", 3: "τριάντα", 4: "σαράντα", 5: "πενήντα",
    6: "εξήντα", 7: "εβδομήντα", 8: "ογδόντα", 9: "ενενήντα",
}
_HUNDREDS = {
    1: "εκατό", 2: "διακόσια", 3: "τριακόσια", 4: "τετρακόσια", 5: "πεντακόσια",
    6: "εξακόσια", 7: "επτακόσια", 8: "οκτακόσια", 9: "εννιακόσια",
}


def _two_digits_to_greek(n: int) -> str:
    if n == 0:
        return ""
    if n < 10:
        return _UNITS[n]
    if n < 20:
        return _TEENS[n]
    tens, units = divmod(n, 10)
    word = _TENS[tens]
    if units:
        word += " " + _UNITS[units]
    return word


def _three_digits_to_greek(n: int) -> str:
    if n == 0:
        return ""
    hundreds, rest = divmod(n, 100)
    parts = []
    if hundreds:
        parts.append(_HUNDREDS[hundreds])
    if rest:
        parts.append(_two_digits_to_greek(rest))
    return " ".join(parts)


def _number_to_greek_words(n: int) -> str:
    """0-9999 -> ελληνικά με λέξεις, π.χ. 2026 -> 'δύο χιλιάδες είκοσι έξι'."""
    if n == 0:
        return "μηδέν"
    thousands, rest = divmod(n, 1000)
    parts = []
    if thousands == 1:
        parts.append("χίλια")
    elif thousands > 1:
        parts.append(_UNITS[thousands] + " χιλιάδες")
    if rest:
        parts.append(_three_digits_to_greek(rest))
    return " ".join(parts)


def _iso_to_greek_full_words(iso: str) -> str:
    """'2026-08-12' -> 'ΔΩΔΕΚΑ ΑΥΓΟΥΣΤΟΥ ΔΥΟ ΧΙΛΙΑΔΕΣ ΕΙΚΟΣΙ ΕΞΙ' -- ολογράφως
    η μέρα ΚΑΙ το έτος, όχι μόνο ο μήνας. Χρησιμοποιείται για ημερομηνία
    γέννησης σε υπεύθυνη δήλωση. Αν δεν είναι έγκυρη ημερομηνία ISO, το
    αφήνουμε όπως είναι."""
    try:
        d = date.fromisoformat(iso)
    except (ValueError, TypeError):
        return iso
    day_words = _number_to_greek_words(d.day)
    month = GREEK_MONTHS[d.month - 1]
    year_words = _number_to_greek_words(d.year)
    return _greek_upper_no_accents(f"{day_words} {month} {year_words}")


# Πεδία ημερομηνίας που πρέπει να γράφονται ΟΛΟΓΡΑΦΩΣ στο τελικό έγγραφο (π.χ.
# "31 Αυγούστου 2026") -- date picker στο UI, μετατροπή από ISO στο backend.
# Μόνο το "hmerominia" (ημερομηνία υπογραφής) παίρνει default τη σημερινή
# ημερομηνία· τα υπόλοιπα μένουν κενά μέχρι να τα συμπληρώσει ο χρήστης,
# γιατί ένα κενό εδώ έχει νόημα (π.χ. "δεν αφορά").
GREEK_DATE_FIELDS = {"hmerominia", "hmerominia_gennisis", "hmerominia_mitroo_pd"}


# Ελληνικές, ΚΕΦΑΛΑΙΕΣ ετικέτες για τα πεδία -- έτσι δείχνει η φόρμα, όχι το
# όνομα μεταβλητής του template. Πρόσθεσε εδώ κάθε νέο πεδίο που φτιάχνεις σε
# template· ό,τι δεν υπάρχει εδώ πέφτει στο αυτόματο fallback (βλ. humanize_label).
GREEK_LABELS: dict[str, str] = {
    "adt": "ΑΡΙΘΜΟΣ ΔΕΛΤΙΟΥ ΤΑΥΤΟΤΗΤΑΣ",
    "afm": "ΑΦΜ",
    "arithmos": "ΑΡΙΘΜΟΣ (ΟΔΟΥ)",
    "arithmos_ergazomenon": "ΑΡΙΘΜΟΣ ΕΡΓΑΖΟΜΕΝΩΝ (ΕΜΕ)",
    "diakritikos_titlos": "ΔΙΑΚΡΙΤΙΚΟΣ ΤΙΤΛΟΣ",
    "dieuthinsi_edras": "ΔΙΕΥΘΥΝΣΗ ΕΤΑΙΡΙΚΗΣ ΕΔΡΑΣ",
    "dieuthintiko_stelexos": "ΟΝΟΜΑ ΚΑΙ ΤΙΤΛΟΣ ΔΙΕΥΘΥΝΤΙΚΟΥ ΣΤΕΛΕΧΟΥΣ",
    "email": "EMAIL",
    "epicheirisi": "ΕΠΩΝΥΜΙΑ ΕΠΙΧΕΙΡΗΣΗΣ",
    "epicheirisi_afm": "ΑΦΜ ΕΠΙΧΕΙΡΗΣΗΣ",
    "epicheirisi_katastatiko": "ΕΠΩΝΥΜΙΑ ΒΑΣΕΙ ΚΑΤΑΣΤΑΤΙΚΟΥ",
    "eponymo": "ΕΠΩΝΥΜΟ",
    "ektasi_dikaiomatos": "ΕΚΤΑΣΗ ΔΙΚΑΙΩΜΑΤΟΣ (ΠΟΣΟΣΤΟ %)",
    "fax": "FAX",
    "gemi": "ΑΡΙΘΜΟΣ ΓΕΜΗ",
    "hmerominia": "ΗΜΕΡΟΜΗΝΙΑ",
    "hmerominia_gennisis": "ΗΜΕΡΟΜΗΝΙΑ ΓΕΝΝΗΣΗΣ",
    "hmerominia_mitroo_pd": "ΗΜΕΡΟΜΗΝΙΑ ΔΗΛΩΣΗΣ ΣΤΟ ΜΗΤΡΩΟ ΠΡΑΓΜΑΤΙΚΩΝ ΔΙΚΑΙΟΥΧΩΝ",
    "kyklos_ergasion": "ΚΥΚΛΟΣ ΕΡΓΑΣΙΩΝ (ΧΙΛ. €)",
    "nomiki_morfi": "ΝΟΜΙΚΗ ΜΟΡΦΗ",
    "nomimos_ekprosopos": "ΟΝΟΜΑΤΕΠΩΝΥΜΟ ΝΟΜΙΜΟΥ ΕΚΠΡΟΣΩΠΟΥ",
    "odos": "ΟΔΟΣ",
    "onoma": "ΟΝΟΜΑ",
    "onoma_eponymo_mitera": "ΟΝΟΜΑ ΚΑΙ ΕΠΩΝΥΜΟ ΜΗΤΕΡΑΣ",
    "onoma_eponymo_patera": "ΟΝΟΜΑ ΚΑΙ ΕΠΩΝΥΜΟ ΠΑΤΕΡΑ",
    "onomateponymo": "ΟΝΟΜΑΤΕΠΩΝΥΜΟ",
    "perigrafi_drastiriotitas": "ΠΕΡΙΓΡΑΦΗ ΔΡΑΣΤΗΡΙΟΤΗΤΑΣ",
    "periodos_anaforas": "ΠΕΡΙΟΔΟΣ ΑΝΑΦΟΡΑΣ",
    "pragmatikoi_dikaiouxoi": "ΠΡΑΓΜΑΤΙΚΟΙ ΔΙΚΑΙΟΥΧΟΙ",
    "pros": "ΠΡΟΣ",
    "sinolo_isologismou": "ΣΥΝΟΛΟ ΙΣΟΛΟΓΙΣΜΟΥ (ΧΙΛ. €)",
    "tilefono": "ΤΗΛΕΦΩΝΟ",
    "tk": "Τ.Κ.",
    "topos": "ΤΟΠΟΣ",
    "topos_gennisis": "ΤΟΠΟΣ ΓΕΝΝΗΣΗΣ",
    "topos_katoikias": "ΤΟΠΟΣ ΚΑΤΟΙΚΙΑΣ",
    "typos_epicheirisis": "ΤΥΠΟΣ ΕΠΙΧΕΙΡΗΣΗΣ",
    "ypikootita": "ΥΠΗΚΟΟΤΗΤΑ",
    "xora_diamonis": "ΧΩΡΑ ΔΙΑΜΟΝΗΣ",
    "ypografon_onoma": "ΟΝΟΜΑΤΕΠΩΝΥΜΟ ΥΠΟΓΡΑΦΟΝΤΟΣ",
    "ypografon_idiotita": "ΙΔΙΟΤΗΤΑ ΥΠΟΓΡΑΦΟΝΤΟΣ",
}

# Πρόσθετοι πραγματικοί δικαιούχοι (πέρα από τον πρώτο/κύριο) στο
# Ερωτηματολόγιο-Υποψηφίου -- ίδια 5 πεδία, επαναλαμβανόμενα με suffix
# _2.._6 (βλ. σχετική ενότητα στο CLAUDE.md). Παράγουμε τις ελληνικές
# ετικέτες τους εδώ αντί να τις γράψουμε 25 φορές με το χέρι.
_PD_BASE_LABELS = {
    "onoma": "ΟΝΟΜΑ", "eponymo": "ΕΠΩΝΥΜΟ", "afm": "ΑΦΜ",
    "adt": "ΑΡΙΘΜΟΣ ΔΕΛΤΙΟΥ ΤΑΥΤΟΤΗΤΑΣ", "ektasi_dikaiomatos": "ΕΚΤΑΣΗ ΔΙΚΑΙΩΜΑΤΟΣ (%)",
}
for _n in range(2, 7):
    for _base, _label in _PD_BASE_LABELS.items():
        GREEK_LABELS[f"{_base}_{_n}"] = f"{_label} ({_n}ος ΔΙΚΑΙΟΥΧΟΣ)"

# Βοηθητικό κείμενο κάτω από την ετικέτα, για πεδία που χρειάζονται λίγη
# καθοδήγηση για να ξέρει ο χρήστης τι/πώς να γράψει.
FIELD_HINTS: dict[str, str] = {
    "ypografon_idiotita": "π.χ. Ιδιοκτήτης, Διαχειριστής, Ομόρρυθμος Εταίρος, Ετερόρρυθμος Εταίρος, Πρόεδρος, Διευθύνων Σύμβουλος",
    "hmerominia_mitroo_pd": "Άφησέ το κενό αν δεν έχεις υποχρέωση εγγραφής (π.χ. ατομική επιχείρηση) — το έγγραφο θα το αναφέρει αυτόματα.",
}

# Πεδία επιλογής (dropdown) -- τιμή που πάει στο template -> τι διαβάζει ο χρήστης.
FIELD_OPTIONS: dict[str, list[dict]] = {
    "typos_epicheirisis": [
        {"value": "anexartiti", "label": "ΑΝΕΞΑΡΤΗΤΗ ΕΠΙΧΕΙΡΗΣΗ"},
        {"value": "synergazomeni", "label": "ΣΥΝΕΡΓΑΖΟΜΕΝΗ ΕΠΙΧΕΙΡΗΣΗ"},
        {"value": "syndedemeni", "label": "ΣΥΝΔΕΔΕΜΕΝΗ ΕΠΙΧΕΙΡΗΣΗ"},
    ],
}

# Οπτικός διαχωρισμός στη φόρμα -- ποια πεδία αφορούν την επιχείρηση, ποια
# τον διαχειριστή/νόμιμο εκπρόσωπο, ποια τους πραγματικούς δικαιούχους. Ό,τι
# δεν αναφέρεται εδώ πέφτει στο "geniko" (fallback group).
GROUP_ORDER = ["epicheirisi", "diaxeiristis", "pragmatikos_dikaiouxos", "geniko"]
GROUP_TITLES = {
    "epicheirisi": "ΣΤΟΙΧΕΙΑ ΕΠΙΧΕΙΡΗΣΗΣ",
    "diaxeiristis": "ΣΤΟΙΧΕΙΑ ΔΙΑΧΕΙΡΙΣΤΗ / ΝΟΜΙΜΟΥ ΕΚΠΡΟΣΩΠΟΥ",
    "pragmatikos_dikaiouxos": "ΠΡΑΓΜΑΤΙΚΟΙ ΔΙΚΑΙΟΥΧΟΙ",
    "geniko": "ΓΕΝΙΚΑ",
}
FIELD_GROUPS: dict[str, str] = {
    # -- Επιχείρηση --
    "epicheirisi": "epicheirisi", "epicheirisi_afm": "epicheirisi",
    "epicheirisi_katastatiko": "epicheirisi", "diakritikos_titlos": "epicheirisi",
    "nomiki_morfi": "epicheirisi", "gemi": "epicheirisi",
    "dieuthinsi_edras": "epicheirisi", "perigrafi_drastiriotitas": "epicheirisi",
    "arithmos_ergazomenon": "epicheirisi", "kyklos_ergasion": "epicheirisi",
    "sinolo_isologismou": "epicheirisi", "periodos_anaforas": "epicheirisi",
    "typos_epicheirisis": "epicheirisi",
    # -- Διαχειριστής / Νόμιμος Εκπρόσωπος --
    "onoma": "diaxeiristis", "eponymo": "diaxeiristis",
    "onoma_eponymo_patera": "diaxeiristis", "onoma_eponymo_mitera": "diaxeiristis",
    "hmerominia_gennisis": "diaxeiristis", "topos_gennisis": "diaxeiristis",
    "adt": "diaxeiristis", "tilefono": "diaxeiristis", "topos_katoikias": "diaxeiristis",
    "odos": "diaxeiristis", "arithmos": "diaxeiristis", "tk": "diaxeiristis",
    "email": "diaxeiristis", "fax": "diaxeiristis", "afm": "diaxeiristis",
    "ypikootita": "diaxeiristis", "xora_diamonis": "diaxeiristis",
    "nomimos_ekprosopos": "diaxeiristis", "onomateponymo": "diaxeiristis",
    "ypografon_onoma": "diaxeiristis", "ypografon_idiotita": "diaxeiristis",
    "dieuthintiko_stelexos": "diaxeiristis",
    # -- Πραγματικοί Δικαιούχοι --
    "ektasi_dikaiomatos": "pragmatikos_dikaiouxos",
    "hmerominia_mitroo_pd": "pragmatikos_dikaiouxos",
    "pragmatikoi_dikaiouxoi": "pragmatikos_dikaiouxos",
}


def field_group(var: str) -> str:
    return FIELD_GROUPS.get(var, "geniko")


def list_templates() -> list[str]:
    if not DOC_TEMPLATES_DIR.exists():
        return []
    return sorted(p.name for p in DOC_TEMPLATES_DIR.glob("*.docx") if not p.name.startswith("~$"))


def template_path(name: str) -> Path:
    # Αποτροπή path traversal -- δεχόμαστε μόνο απλό filename.
    if "/" in name or "\\" in name or not name.endswith(".docx"):
        raise HTTPException(400, "Μη έγκυρο όνομα template")
    p = DOC_TEMPLATES_DIR / name
    if not p.exists():
        raise HTTPException(404, f"Δεν βρέθηκε το template: {name}")
    return p


def humanize_label(var: str) -> str:
    """Ελληνική, ΚΕΦΑΛΑΙΑ ετικέτα για το πεδίο. Πρώτα κοιτάμε το GREEK_LABELS·
    αν το πεδίο είναι άγνωστο (νέο template), fallback σε κεφαλαία με κενά
    αντί για underscore -- καλύτερο από τίποτα, αλλά πρόσθεσε το στο
    GREEK_LABELS για σωστή ελληνική ετικέτα."""
    if var in GREEK_LABELS:
        return GREEK_LABELS[var]
    return var.replace("_", " ").strip().upper()


def guess_field_type(var: str) -> str:
    if var in FIELD_OPTIONS:
        return "select"
    v = var.lower()
    if var in GREEK_DATE_FIELDS:
        return "date-greek"
    if v.startswith("ektasi_dikaiomatos"):
        return "percent"
    if "date" in v or "imerom" in v or "hmerom" in v:
        return "date"
    if "keimeno" in v or "text" in v or "perigraf" in v or "dilosi" in v:
        return "textarea"
    return "text"


@app.get("/api/templates")
def api_list_templates():
    return {"templates": list_templates()}


def _default_for(var: str, field_type: str) -> str:
    if field_type == "date-greek" and var == "hmerominia":
        return date.today().isoformat()
    if field_type == "date" and "today" in var.lower():
        return _today_greek()
    return ""


def _fields_for_template(name: str) -> list[dict]:
    path = template_path(name)
    doc = DocxTemplate(str(path))
    variables = sorted(doc.get_undeclared_template_variables())
    fields = []
    for var in variables:
        field_type = guess_field_type(var)
        field = {
            "name": var,
            "label": humanize_label(var),
            "type": field_type,
            "default": _default_for(var, field_type),
            "group": field_group(var),
        }
        if field_type == "select":
            field["options"] = FIELD_OPTIONS[var]
        if var in FIELD_HINTS:
            field["hint"] = FIELD_HINTS[var]
        fields.append(field)
    return fields


@app.get("/api/templates/{name}/fields")
def api_template_fields(name: str):
    return {"template": name, "fields": _fields_for_template(name)}


@app.get("/api/fields")
def api_all_fields():
    """Ένωση όλων των πεδίων απ' όλα τα templates (dedup βάσει ονόματος
    μεταβλητής), ώστε το UI να δείχνει μία κοινή φόρμα για να τα γεμίζεις
    όλα μαζί σε ένα πέρασμα."""
    seen: dict[str, dict] = {}
    for name in list_templates():
        for f in _fields_for_template(name):
            seen.setdefault(f["name"], f)
    fields = sorted(seen.values(), key=lambda f: f["name"])
    groups = [{"key": k, "title": GROUP_TITLES[k]} for k in GROUP_ORDER]
    return {"fields": fields, "templates": list_templates(), "groups": groups}


def _render_template_bytes(name: str, values: dict[str, str]) -> bytes:
    path = template_path(name)
    doc = DocxTemplate(str(path))
    # Συμπληρώνουμε κενές τιμές με "" ώστε να μη σκάει το render αν λείπει κάτι.
    variables = doc.get_undeclared_template_variables()
    context = {var: values.get(var, "") for var in variables}
    # Τα πεδία ημερομηνίας που πρέπει να είναι ολογράφως έρχονται από date
    # picker σε μορφή ISO (YYYY-MM-DD) -- τα μετατρέπουμε πριν το render.
    # Η ημερομηνία γέννησης θέλει ΠΛΗΡΩΣ ολογράφως (μέρα ΚΑΙ έτος με λέξεις,
    # κεφαλαία) -- π.χ. "ΔΩΔΕΚΑ ΑΥΓΟΥΣΤΟΥ ΔΥΟ ΧΙΛΙΑΔΕΣ ΕΙΚΟΣΙ ΕΞΙ". Οι άλλες
    # ημερομηνίες (υπογραφής, μητρώου ΠΔ) μένουν στη συνηθισμένη μορφή
    # "31 Αυγούστου 2026".
    if context.get("hmerominia_gennisis"):
        context["hmerominia_gennisis"] = _iso_to_greek_full_words(context["hmerominia_gennisis"])
    for var in GREEK_DATE_FIELDS - {"hmerominia_gennisis"}:
        if context.get(var):
            context[var] = _iso_to_greek(context[var])
    doc.render(context)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def _out_name(template_name: str) -> str:
    safe_name = re.sub(r"\.docx$", "", template_name)
    return f"{safe_name}_συμπληρωμένο.docx"


class GenerateRequest(BaseModel):
    template: str
    values: dict[str, str]


@app.post("/api/generate")
def api_generate(req: GenerateRequest):
    data = _render_template_bytes(req.template, req.values)
    out_name = _out_name(req.template)

    # Content-Disposition δεν δέχεται non-ASCII στο filename="" -- βάζουμε ASCII
    # fallback και το πραγματικό (με ελληνικά) μέσω filename*=UTF-8''...
    ascii_fallback = "ypeuthini_dilosi.docx"
    from urllib.parse import quote
    encoded = quote(out_name)

    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'
            )
        },
    )


class GenerateAllRequest(BaseModel):
    values: dict[str, str]
    templates: Optional[list[str]] = None  # None = όλα τα διαθέσιμα templates


@app.post("/api/generate-all")
def api_generate_all(req: GenerateAllRequest):
    names = req.templates if req.templates else list_templates()
    if not names:
        raise HTTPException(400, "Δεν υπάρχουν templates προς δημιουργία")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in names:
            data = _render_template_bytes(name, req.values)
            zf.writestr(_out_name(name), data)
    zip_buf.seek(0)

    from urllib.parse import quote
    out_name = "Υπεύθυνες_Δηλώσεις.zip"
    encoded = quote(out_name)

    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f'attachment; filename="dilosis.zip"; filename*=UTF-8\'\'{encoded}'
            )
        },
    )


@app.get("/", response_class=HTMLResponse)
def index():
    return (UI_DIR / "index.html").read_text(encoding="utf-8")
