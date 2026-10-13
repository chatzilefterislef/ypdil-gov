"""
Τοπικό OCR (RapidOCR + ελληνικό μοντέλο, χωρίς internet/API) που διαβάζει
έγγραφα/screenshots και επιστρέφει τιμές για τα πεδία της φόρμας.

Τρία είδη εγγράφων:
  - "company": Βεβαίωση Τρέχουσας Εικόνας Οντότητας/Επιχείρησης (myAADE, PDF ή εικόνα)
  - "person":  screenshot φόρμας προσωπικών στοιχείων (gov.gr υπεύθυνη δήλωση)

Τίποτα δεν αποθηκεύεται: τα bytes μένουν στη μνήμη όσο διαρκεί το αίτημα.
"""
from __future__ import annotations

import io
import re
import threading
import unicodedata
import warnings
from datetime import datetime
from difflib import SequenceMatcher
from pathlib import Path

warnings.filterwarnings("ignore")

MODEL_DIR = Path(__file__).resolve().parent / "models"
MAX_PDF_PAGES = 3
PDF_SCALE = 2.5

_engine = None
_engine_lock = threading.Lock()


def _get_engine():
    global _engine
    with _engine_lock:
        if _engine is None:
            from rapidocr import RapidOCR, LangRec, ModelType, OCRVersion
            _engine = RapidOCR(params={
                "Global.log_level": "error",
                "Rec.lang_type": LangRec.EL,
                "Rec.ocr_version": OCRVersion.PPOCRV5,
                "Rec.model_type": ModelType.MOBILE,
                "Rec.model_path": str(MODEL_DIR / "el_PP-OCRv5_rec_mobile.onnx"),
            })
        return _engine


def warm_up() -> None:
    """Φορτώνει το μοντέλο στο παρασκήνιο, ώστε το πρώτο αίτημα να μην περιμένει."""
    try:
        _get_engine()
    except Exception:
        pass


class Box:
    __slots__ = ("text", "x0", "y0", "x1", "y1")

    def __init__(self, text, x0, y0, x1, y1):
        self.text, self.x0, self.y0, self.x1, self.y1 = text, x0, y0, x1, y1

    @property
    def h(self):
        return self.y1 - self.y0


def load_images(filename: str, data: bytes):
    """PDF -> εικόνες σελίδων (πρώτες MAX_PDF_PAGES)· εικόνα -> [εικόνα]."""
    from PIL import Image
    if data[:5] == b"%PDF-":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(data)
        n = min(len(pdf), MAX_PDF_PAGES)
        return [pdf[i].render(scale=PDF_SCALE).to_pil().convert("RGB") for i in range(n)]
    return [Image.open(io.BytesIO(data)).convert("RGB")]


def ocr_boxes(image) -> list[Box]:
    import numpy as np
    result = _get_engine()(np.array(image))
    if result.txts is None or result.boxes is None:
        return []
    boxes = []
    for txt, pts in zip(result.txts, result.boxes):
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        boxes.append(Box(txt.strip(), min(xs), min(ys), max(xs), max(ys)))
    # σειρά ανάγνωσης: πάνω->κάτω, αριστερά->δεξιά
    boxes.sort(key=lambda b: (round(b.y0 / max(b.h, 1)), b.x0))
    return boxes


# ---------------------------------------------------------------- helpers
_LATIN_TO_GREEK = str.maketrans("ABEHIKMNOPTXYZ", "ΑΒΕΗΙΚΜΝΟΡΤΧΥΖ")


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFD", s.upper())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.translate(_LATIN_TO_GREEK)
    return re.sub(r"\s+", " ", re.sub(r"[^A-ZΑ-Ω0-9 ]", "", s)).strip()


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


def _iso_date(s: str) -> str:
    m = re.search(r"(\d{1,2})\s*[/\-.]\s*(\d{1,2})\s*[/\-.]\s*(\d{4})", s)
    if not m:
        return ""
    try:
        return datetime(int(m.group(3)), int(m.group(2)), int(m.group(1))).date().isoformat()
    except ValueError:
        return ""


# ---------------------------------------------------- Βεβαίωση myAADE (company)
_CERT_LABELS = {
    "ΕΠΩΝΥΜΙΑ": "name", "ΑΦΜ": "afm", "ΝΟΜΙΚΗ ΜΟΡΦΗ": "legal_form",
    "ΔΝΣΗ ΑΣΚΗΣΗΣ ΔΡΑΣΤΗΡΙΟΤΗΤΑΣ": "address",
}
_CERT_OTHER_LABELS = [
    "Αρμόδια Υπηρεσία", "Προέλευση Έναρξης", "Ημνία Έναρξης", "Τίτλος Επιχείρησης",
    "Κατάσταση Επιχείρησης", "Κατηγορία Βιβλίων", "Τρόπος Ένταξης", "Λήξη Διαχειριστικής Περιόδου",
    "Υπαγωγή ΦΠΑ", "Καθεστώς ΦΠΑ", "Κατηγορία Οντότητας", "ΕΦΚ", "Αριθμός εγγραφής",
]


def _is_label(text: str) -> bool:
    n = _norm(text)
    if any(_sim(n, k) >= 0.85 for k in list(_CERT_LABELS) + _CERT_OTHER_LABELS):
        return True
    return False


def _value_right_of(label: Box, boxes: list[Box]):
    """Η τιμή στην ίδια γραμμή, δεξιά από την ετικέτα (πίνακας δύο στηλών)."""
    cy = (label.y0 + label.y1) / 2
    cands = [
        b for b in boxes
        if b is not label and b.x0 > label.x1
        and abs((b.y0 + b.y1) / 2 - cy) <= label.h * 0.9
    ]
    if not cands:
        return None
    nxt = min(cands, key=lambda b: b.x0)
    return None if _is_label(nxt.text) else nxt


def parse_company_cert(pages: list[list[Box]]) -> dict[str, str]:
    found: dict[str, str] = {}
    for boxes in pages:
        for label_text, key in _CERT_LABELS.items():
            if key in found:
                continue
            matches = [b for b in boxes if _sim(b.text, label_text) >= 0.85]
            for lab in sorted(matches, key=lambda b: b.y0):
                val = _value_right_of(lab, boxes)
                if val and val.text.strip():
                    found[key] = val.text.strip()
                    break
    out: dict[str, str] = {}
    if found.get("name"):
        out["epicheirisi"] = found["name"]
        out["epicheirisi_katastatiko"] = found["name"]
    afm = re.sub(r"\D", "", found.get("afm", ""))
    if len(afm) == 9:
        out["epicheirisi_afm"] = afm
    if found.get("legal_form"):
        out["nomiki_morfi"] = found["legal_form"]
    if found.get("address"):
        out["dieuthinsi_edras"] = found["address"]
    return out



# ------------------------------- πίνακας «ΜΕΛΗ/ΕΤΑΙΡΟΙ» της Βεβαίωσης myAADE
def _split_name(full: str) -> tuple[str, str]:
    """Στο myAADE το «Ονοματεπώνυμο» είναι ΕΠΩΝΥΜΟ πρώτα, μετά ΟΝΟΜΑ."""
    parts = full.split()
    if len(parts) <= 1:
        return full.strip(), ""
    return parts[0], " ".join(parts[1:])


def _pct(text: str) -> str:
    m = re.fullmatch(r"(\d{1,3})[,.](\d{1,2})", text.strip())
    if not m:
        return ""
    whole, dec = m.group(1), m.group(2).rstrip("0")
    return f"{whole}.{dec}" if dec else whole


def parse_members(pages: list[list[Box]]) -> list[dict[str, str]]:
    members: list[dict[str, str]] = []
    for boxes in pages:
        start = next((b for b in boxes if "ΜΕΛΗ" in _norm(b.text) and "ΕΤΑΙΡ" in _norm(b.text)), None)
        if not start:
            continue
        end = next((b for b in boxes if b.y0 > start.y1 and "ΔΡΑΣΤΗΡΙΟΤΗΤ" in _norm(b.text)), None)
        y_end = end.y0 if end else float("inf")
        region = [b for b in boxes if start.y1 < b.y0 < y_end]
        anchors = [b for b in region if re.fullmatch(r"\d{9}", b.text.strip())]
        if not anchors:
            continue
        top = min(a.y0 for a in anchors) - anchors[0].h * 0.6
        region = [b for b in region if (b.y0 + b.y1) / 2 >= top]   # όχι οι επικεφαλίδες στηλών
        groups: dict[int, list[Box]] = {i: [] for i in range(len(anchors))}
        for b in region:
            cy = (b.y0 + b.y1) / 2
            i = min(range(len(anchors)), key=lambda k: abs((anchors[k].y0 + anchors[k].y1) / 2 - cy))
            groups[i].append(b)
        for i, a in enumerate(anchors):
            g = groups[i]
            pct = next((_pct(b.text) for b in g if _pct(b.text)), "")
            names = [
                b.text for b in g
                if b is not a and not re.search(r"\d", b.text) and not _norm(b.text).startswith("ΔΟΥ")
                and not any(w in _norm(b.text) for w in ("ΜΕΛΟΣ", "ΕΤΑΙΡΟΣ", "ΔΙΑΧΕΙΡΙΣΤΗΣ", "ΥΠΗΡΕΣΙΑ"))
            ]
            if not names:
                continue
            surname, name = _split_name(max(names, key=len))
            members.append({"afm": a.text.strip(), "eponymo": surname, "onoma": name, "ektasi_dikaiomatos": pct})
    return members


# --------------- Βεβαίωση Βασικών Στοιχείων Φυσικού Προσώπου (myAADE)
_PERSON_CERT_LABELS = {
    "ΑΦΜ": "afm",
    "ΕΠΩΝΥΜΟ ΕΠΩΝΥΜΟΒ ΟΝΟΜΑ": "fullname",
    "ΕΠΩΝΥΜΟ ΟΝΟΜΑ ΠΑΤΡΟΣ": "father",
    "ΕΠΩΝΥΜΟ ΟΝΟΜΑ ΜΗΤΕΡΑΣ": "mother",
    "ΗΜΝΙΑ ΓΕΝΝΗΣΗΣ": "birth_date",
    "ΤΟΠΟΣ ΧΩΡΑ ΓΕΝΝΗΣΗΣ": "topos_gennisis",
    "ΥΠΗΚΟΟΤΗΤΑ": "ypikootita",
    "ΔΙΕΥΘΥΝΣΗ ΚΑΤΟΙΚΙΑΣ": "address",
    "ΑΡ ΤΑΥΤΟΤΗΤΑΣ": "adt",
    "ΑΡΜΟΔΙΑ ΥΠΗΡΕΣΙΑ": "doy",
    "ΟΙΚΟΓΕΝΕΙΑΚΗ ΚΑΤΑΣΤΑΣΗ": "family",
    "ΕΙΔΟΣ ΤΑΥΤΟΤΗΤΑΣ": "id_kind",
    "ΗΜΝΙΑ ΕΚΔΟΣΗΣ": "issue_date",
    "ΕΚΔΟΥΣΑ ΑΡΧΗ": "issuer",
}


def is_person_certificate(boxes: list[Box]) -> bool:
    return any("ΦΥΣΙΚΟΥ ΠΡΟΣΩΠΟΥ" in _norm(b.text) and "ΒΑΣΙΚΩΝ" in _norm(b.text) for b in boxes)


def is_company_certificate(boxes: list[Box]) -> bool:
    return any("ΟΝΤΟΤΗΤΑΣ" in _norm(b.text) and "ΒΕΒΑΙΩΣΗ" in _norm(b.text) for b in boxes)


def _name_first_to_last(full: str) -> str:
    """«ΕΠΩΝΥΜΟ ΟΝΟΜΑ» (myAADE) -> «ΟΝΟΜΑ ΕΠΩΝΥΜΟ» (όπως τα θέλει η φόρμα)."""
    parts = full.split()
    return " ".join(parts[1:] + parts[:1]) if len(parts) > 1 else full.strip()


def parse_person_certificate(boxes: list[Box]) -> dict[str, str]:
    found: dict[str, str] = {}
    for b in boxes:
        n = _norm(b.text)
        if not n or len(n) > 40:
            continue
        key, score = None, 0.0
        for label, k in _PERSON_CERT_LABELS.items():
            s = SequenceMatcher(None, n, label).ratio()
            if s > score:
                key, score = k, s
        if key and score >= 0.85 and key not in found:
            val = _value_right_of(b, boxes)
            if val and val.text.strip():
                found[key] = val.text.strip()
    out: dict[str, str] = {}
    if found.get("fullname"):
        out["eponymo"], out["onoma"] = _split_name(found["fullname"])
    if found.get("father"):
        out["onoma_eponymo_patera"] = _name_first_to_last(found["father"])
    if found.get("mother"):
        out["onoma_eponymo_mitera"] = _name_first_to_last(found["mother"])
    afm = re.sub(r"\D", "", found.get("afm", ""))
    if len(afm) == 9:
        out["afm"] = afm
    iso = _iso_date(found.get("birth_date", ""))
    if iso:
        out["hmerominia_gennisis"] = iso
    for k in ("topos_gennisis", "ypikootita", "adt"):
        if found.get(k):
            out[k] = found[k].replace(" ", "") if k == "adt" else found[k]
    addr = found.get("address", "")
    m = re.search(r"(?:Τ\.?Κ\.?|TK)\s*:?\s*(\d{5})\s*(.*)$", addr, re.I)
    if m:
        out["tk"] = m.group(1)
        if m.group(2).strip():
            out["topos_katoikias"] = m.group(2).strip()
        street = re.sub(r"\s+Ο\s*$", "", addr[:m.start()].strip())
        if street:
            out["odos"] = street
    elif addr:
        out["odos"] = addr
    return out


# ------------------------------------------- screenshot προσωπικών στοιχείων
_PERSON_LABELS = {
    "onoma": "ΟΝΟΜΑ", "eponymo": "ΕΠΩΝΥΜΟ",
    "onoma_eponymo_patera": "ΟΝΟΜΑ ΚΑΙ ΕΠΩΝΥΜΟ ΠΑΤΕΡΑ",
    "onoma_eponymo_mitera": "ΟΝΟΜΑ ΚΑΙ ΕΠΩΝΥΜΟ ΜΗΤΕΡΑΣ",
    "hmerominia_gennisis": "ΗΜΕΡΟΜΗΝΙΑ ΓΕΝΝΗΣΗΣ", "topos_gennisis": "ΤΟΠΟΣ ΓΕΝΝΗΣΗΣ",
    "adt": "ΑΡΙΘΜΟΣ ΔΕΛΤΙΟΥ ΤΑΥΤΟΤΗΤΑΣ", "afm": "ΑΦΜ",
    "topos_katoikias": "ΤΟΠΟΣ ΚΑΤΟΙΚΙΑΣ", "odos": "ΟΔΟΣ", "arithmos": "ΑΡΙΘΜΟΣ", "tk": "ΤΚ",
    "tilefono": "ΤΗΛΕΦΩΝΟ", "email": "ΔΝΣΗ ΗΛΕΚΤΡ ΤΑΧΥΔΡΟΜΕΙΟΥ EMAIL",
}
_PERSON_HEADINGS = ["ΔΙΕΥΘΥΝΣΗ ΚΑΤΟΙΚΙΑΣ", "ΣΤΟΙΧΕΙΑ ΕΠΙΚΟΙΝΩΝΙΑΣ", "ΠΕΔΙΟ ΜΟΝΟ ΓΙΑ ΠΡΟΒΟΛΗ"]


def _person_label_key(text: str):
    n = _norm(text)
    if not n:
        return None
    # "Δ/νση Ηλεκτρ. Ταχυδρομείου (E-mail)": αρκεί η αρχή
    if n.startswith("ΔΝΣΗ ΗΛΕΚΤΡ") or n.startswith("ΔΝΣΗ ΗΛΕΚ"):
        return "email"
    best, best_s = None, 0.0
    for key, label in _PERSON_LABELS.items():
        s = SequenceMatcher(None, n, label).ratio()
        if s > best_s:
            best, best_s = key, s
    return best if best_s >= 0.86 else None


def _is_heading(text: str) -> bool:
    n = _norm(text)
    return any(SequenceMatcher(None, n, h).ratio() >= 0.85 for h in _PERSON_HEADINGS) or n.startswith("ΠΕΔΙΟ ΜΟΝΟ")


def parse_person(boxes: list[Box]) -> dict[str, str]:
    labels = []  # (key, box)
    for b in boxes:
        k = _person_label_key(b.text)
        if k:
            labels.append((k, b))
    # κάθε label κρατιέται μία φορά (το πιο πάνω)
    seen, uniq = set(), []
    for k, b in sorted(labels, key=lambda kb: kb[1].y0):
        if k not in seen:
            seen.add(k)
            uniq.append((k, b))
    label_ids = {id(b) for _, b in uniq}

    out: dict[str, str] = {}
    for key, lab in uniq:
        tol = max(lab.h * 2.0, 20)
        below = [
            b for b in boxes
            if b is not lab and b.y0 >= lab.y1 - lab.h * 0.3
            and abs(b.x0 - lab.x0) <= tol and b.y0 - lab.y1 <= lab.h * 4
        ]
        if not below:
            continue
        nxt = min(below, key=lambda b: b.y0)
        if id(nxt) in label_ids or _is_heading(nxt.text) or _person_label_key(nxt.text):
            continue  # άδειο πεδίο: από κάτω είναι άλλη ετικέτα
        out[key] = nxt.text.strip()

    # Εναλλακτικές διατάξεις (PDF/εκτύπωση): τιμή δεξιά στην ίδια γραμμή ή «Ετικέτα: τιμή».
    for key, lab in uniq:
        if key in out:
            continue
        cy = (lab.y0 + lab.y1) / 2
        right = [b for b in boxes if b is not lab and b.x0 >= lab.x1 - 2
                 and abs((b.y0 + b.y1) / 2 - cy) <= lab.h * 0.8]
        if right:
            nxt = min(right, key=lambda b: b.x0)
            if not _person_label_key(nxt.text) and not _is_heading(nxt.text):
                out[key] = nxt.text.strip()
    for b in boxes:
        if ":" not in b.text:
            continue
        head, _, tail = b.text.partition(":")
        k = _person_label_key(head)
        if k and k not in out and tail.strip():
            out[k] = tail.strip()

    # Ετικέτα που το OCR δεν διάβασε: τηλέφωνο/email αναγνωρίζονται από τη μορφή τους.
    if "tilefono" not in out:
        phones = [b.text for b in boxes if re.fullmatch(r"\+?(30)?\s?(69\d{8}|2\d{9})", re.sub(r"[\s\-]", "", b.text))
                  or re.fullmatch(r"\+30(69\d{8}|2\d{9})", re.sub(r"[\s\-]", "", b.text))]
        if len(phones) == 1:
            out["tilefono"] = phones[0]
    if "email" not in out:
        mails = [b.text for b in boxes if re.fullmatch(r"[\w.+-]+@[\w-]+(\.[\w-]+)+", b.text.strip())]
        if len(mails) == 1:
            out["email"] = mails[0].strip()

    if "hmerominia_gennisis" not in out:
        # η ετικέτα δεν διαβάστηκε -- αρκεί η μοναδική ημερομηνία της εικόνας
        dates = [b.text for b in boxes if _iso_date(b.text)]
        if len(dates) == 1:
            out["hmerominia_gennisis"] = dates[0]
    if "hmerominia_gennisis" in out:
        iso = _iso_date(out["hmerominia_gennisis"])
        if iso:
            out["hmerominia_gennisis"] = iso
        else:
            del out["hmerominia_gennisis"]
    for k in ("afm",):
        if k in out:
            out[k] = re.sub(r"\D", "", out[k])
    if "tilefono" in out:
        out["tilefono"] = re.sub(r"\s", "", out["tilefono"])
    return {k: v for k, v in out.items() if v}


def extract(kind: str, filename: str, data: bytes) -> dict:
    """Επιστρέφει {"fields": {...}} και, για kind="owners", και "members": [...]."""
    images = load_images(filename, data)
    if kind == "company":
        pages = [ocr_boxes(img) for img in images]
        if any(is_person_certificate(b) for b in pages):                 # λάθος είδος εγγράφου -> τίποτα
            return {"fields": {}, "members": []}
        return {"fields": parse_company_cert(pages), "members": parse_members(pages)}
    if kind in ("person", "owners"):
        pages = [ocr_boxes(img) for img in images]
        if kind == "owners":
            members = parse_members(pages)
            if members:
                return {"fields": {}, "members": members}
        merged: dict[str, str] = {}
        if any(is_company_certificate(b) for b in pages):
            return {"fields": {}}       # μητρώο επιχείρησης σε σημείο προσώπου (όλες οι σελίδες του)
        for boxes in pages:
            parsed = parse_person_certificate(boxes) if is_person_certificate(boxes) else parse_person(boxes)
            for k, v in parsed.items():
                merged.setdefault(k, v)
        return {"fields": merged}
    raise ValueError(f"άγνωστο είδος εγγράφου: {kind}")
