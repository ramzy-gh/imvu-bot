"""Offline intent matching; no network, model, or API key required."""
import gzip
import json
import random
import re
import unicodedata
from collections import OrderedDict, defaultdict
from difflib import SequenceMatcher
from pathlib import Path

ROOT = Path(__file__).resolve().parent
NAMES = re.compile(r"(?<!\w)(?:ramsei|رامسي|رامزى|رامزي)(?!\w)", re.I)

# These add polite conversational phrasing, not new semantic topics.
PREFIXES = {
    "en": ["", "hey ramsei ", "ramsei ", "hello ramsei ", "hi ramsei ",
           "quick question ", "a quick question ", "just wondering ", "so ",
           "well ", "by the way ", "hey there ", "please ", "listen ramsei ",
           "okay ramsei ", "can i ask something "],
    "ar": ["", "رامسي ", "يا رامسي ", "بقولك ", "بقولك يا رامسي ",
           "بص ", "طب ", "طيب ", "هو ", "سؤال صغير ", "على فكرة ",
           "يا نجم ", "معلش ", "ممكن سؤال ", "لو سمحت ", "اسمع يا رامسي "],
    "arabizi": ["", "ramsei ", "ya ramsei ", "ba2olak ", "tab ",
                "tayeb ", "law sama7t ", "momken so2al "],
}
SUFFIXES = {
    "en": ["", " please", " ramsei", " if you can", " thanks", " just curious",
           " what do you think", " tell me", " honestly", " my friend", " for a moment",
           " if thats okay", " when you have a moment", " lets chat", " im curious",
           " id like to know"],
    "ar": ["", " يا رامسي", " لو سمحت", " يا نجم", " بقى", " كده",
           " قولي", " بصراحة", " يا صاحبي", " يلا", " عشان عندي فضول",
           " لما تفضى", " لو تقدر", " يا معلم", " خلينا نرغي", " معلش"],
    "arabizi": ["", " ya ramsei", " law sama7t", " ya negm", " keda", " ba2a",
                " besara7a", " ya sa7by"],
}


def clean(text):
    text = unicodedata.normalize("NFKC", text).casefold().replace("’", "'")
    text = re.sub(r"[\u064b-\u065f\u0670\u0640]", "", text)
    text = text.translate(str.maketrans("أإآىة", "ااايه"))
    text = text.replace("'", "")
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def load_topics(path=None):
    topics = []
    for line in (path or ROOT / "offline_seed.txt").read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 6:
            raise ValueError("Invalid offline topic row")
        key, en, ar, arabizi, replies_en, replies_ar = parts
        topics.append({"id": key, "questions": {"en": en.split(";"), "ar": ar.split(";"),
                       "arabizi": arabizi.split(";")},
                       "replies": {"en": replies_en.split("~"), "ar": replies_ar.split("~")}})
    if len({t["id"] for t in topics}) != len(topics):
        raise ValueError("Duplicate offline topic")
    return topics


class OfflineChat:
    def __init__(self, path=None):
        self.topics = {t["id"]: t for t in load_topics(path)}
        self.exact = {}
        self.inverted = defaultdict(set)
        self.recent = OrderedDict()
        self.people = OrderedDict()
        self.last_match = None
        for topic in self.topics.values():
            for lang, questions in topic["questions"].items():
                for question in questions:
                    q = clean(NAMES.sub("", question))
                    self.exact.setdefault(q, (topic["id"], lang))
                    for token in q.split():
                        self.inverted[token].add(q)
        self.prefixes = sorted({clean(NAMES.sub("", p)) for ps in PREFIXES.values() for p in ps} - {""}, key=len, reverse=True)
        self.suffixes = sorted({clean(NAMES.sub("", p)) for ps in SUFFIXES.values() for p in ps} - {""}, key=len, reverse=True)

    def match(self, text):
        q = clean(NAMES.sub("", text[:1000]))
        if q in self.exact:
            return self.exact[q]
        # Consider each wrapper interpretation: greedy stripping can eat a real
        # question (e.g. 'hello' is both a greeting and a polite prefix).
        forms = {q}
        for _ in range(2):
            for form in tuple(forms):
                forms.update(form[len(prefix):].strip() for prefix in self.prefixes if form.startswith(prefix + " "))
            for form in tuple(forms):
                forms.update(form[:-len(suffix)].strip() for suffix in self.suffixes if form.endswith(" " + suffix))
        matches = [self.exact[form] for form in forms if form in self.exact]
        if matches:
            return matches[0] if len({m[0] for m in matches}) == 1 else None
        q = min(forms, key=len)
        if len(q) < 5 or len(q.split()) < 2:
            return None
        # Avoid inverting the meaning of an unknown negated statement.
        if re.search(r"\b(?:not|never|dont|cant|مش|مبقتش|مليش)\b", q):
            return None
        tokens = set(q.split())
        candidates = set().union(*(self.inverted.get(t, set()) for t in tokens))
        scored = []
        for candidate in candidates:
            target = set(candidate.split())
            overlap = len(tokens & target)
            if overlap / max(len(tokens), len(target)) < .5:
                continue
            ratio = SequenceMatcher(None, q, candidate).ratio()
            score = .65 * ratio + .35 * (2 * overlap / (len(tokens) + len(target)))
            if score >= .84:
                scored.append((score, self.exact[candidate]))
        scored.sort(reverse=True)
        if not scored:
            return None
        if any(result[0] != scored[0][1][0] and scored[0][0] - score < .035 for score, result in scored[1:]):
            return None
        return scored[0][1]

    def pick(self, uid, topic, language):
        choices = self.topics[topic]["replies"][language]
        previous = self.recent.get(uid)
        available = [c for c in choices if c != previous] or choices
        result = random.choice(available)
        self.recent[uid] = result
        self.recent.move_to_end(uid)
        while len(self.recent) > 300:
            self.recent.popitem(last=False)
        return result

    def reply(self, uid, text, language="en"):
        language = "ar" if language == "ar" else "en"
        raw = NAMES.sub("", text[:1000]).strip(" ,!؟?")
        if not clean(raw):
            self.last_match = "presence"
            return self.pick(uid, "presence", language)
        name = re.fullmatch(r"(?:my name is|call me|اسمي|ناديني)\s+([\w\u0621-\u064a]{2,24}(?: [\w\u0621-\u064a]{2,24})?)", raw, re.I)
        if name:
            value = name[1]
            self.people[uid] = value
            self.people.move_to_end(uid)
            while len(self.people) > 300:
                self.people.popitem(last=False)
            self.last_match = "name_memory"
            return f"أهلاً يا {value} 😄 هفتكر اسمك طول التشغيل الحالي. تحب نرغي في إيه؟" if language == "ar" else f"Nice to meet you, {value} 😄 I'll remember that during this session. What shall we chat about?"
        if clean(raw) in {"what is my name", "whats my name", "do you know my name", "اسمي ايه", "فاكر اسمي"}:
            value = self.people.get(uid)
            self.last_match = "name_recall"
            return ((f"اسمك {value} 😄" if language == "ar" else f"You told me to call you {value} 😄") if value else
                    ("لسه مقولتليش اسمك 🙂 قول اسمي وبعدها الاسم، من غير بيانات خاصة." if language == "ar" else "You haven't told me a name yet 🙂 Say 'call me' and a nickname; no private details needed."))
        if clean(raw) in {"forget my name", "forget me", "انسي اسمي", "امسح اسمي", "انساني"}:
            self.people.pop(uid, None)
            self.recent.pop(uid, None)
            self.last_match = "forget_name"
            return "مسحت الاسم من ذاكرتي المؤقتة 🙂" if language == "ar" else "I've cleared your name from my temporary memory 🙂"
        match = self.match(raw)
        if match:
            topic, matched_language = match
            # Arabizi phrases intentionally receive Egyptian Arabic.
            language = "ar" if matched_language == "arabizi" else language
            self.last_match = topic
            return self.pick(uid, topic, language)
        self.last_match = "unknown"
        options = (["مش لاقط السؤال ده في ردودي المكتوبة 😅 جرب صياغة أبسط، أو نرغي عن أغاني وألعاب وأكل وهوايات؟",
                    "السؤال ده محتاج فهم أكتر من السكريبت بتاعي 🙂 قول موضوع واضح زي مزيكا أو أفلام أو سفر.",
                    "ممكن تبسطهولي؟ 😄 أنا أوفلاين، بس جاهز للرغي المعروف و !game و !play."] if language == "ar" else
                   ["That one's outside my written replies 😅 Try a simpler question, or let's chat music, games, food, or hobbies.",
                    "I don't have a reliable scripted answer for that 🙂 Name a clear topic like movies, travel, or music.",
                    "Could you rephrase that? 😄 I'm offline, but familiar topics, !game and !play are ready."])
        return random.choice([o for o in options if o != self.recent.get(uid)] or options)


def build_bank(destination=ROOT):
    topics = load_topics()
    matcher = OfflineChat()
    destination = Path(destination)
    seen = set()
    counts = defaultdict(int)
    words = 0
    ambiguous = 0
    with gzip.open(destination / "offline_questions.jsonl.gz", "wt", encoding="utf-8", newline="\n") as stream:
        for topic in topics:
            for lang, questions in topic["questions"].items():
                for question in questions:
                    for prefix in PREFIXES[lang]:
                        for suffix in SUFFIXES[lang]:
                            text = prefix + question + suffix
                            normalized = clean(text)
                            if normalized in seen:
                                continue
                            match = matcher.match(text)
                            if not match or match[0] != topic["id"]:
                                ambiguous += 1
                                continue
                            seen.add(normalized)
                            counts[lang] += 1
                            words += len(text.split())
                            stream.write(json.dumps({"topic": topic["id"], "language": lang, "question": text}, ensure_ascii=False) + "\n")
    replies = [r for t in topics for rs in t["replies"].values() for r in rs]
    report = {"version": "10", "topics": len(topics), "seed_questions": sum(len(q) for t in topics for q in t["questions"].values()),
              "generated_unique_phrasings": len(seen), "phrasings_by_language": dict(counts),
              "question_word_count": words, "written_replies": len(replies),
              "reply_word_count": sum(len(r.split()) for r in replies),
              "excluded_ambiguous_candidates": ambiguous,
              "generation": "Compositional polite prefixes/suffixes around curated questions; not separate semantic topics.",
              "runtime": "Loads only offline_seed.txt; generated bank is an inspectable corpus, not a large memory index."}
    (destination / "offline_stats.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(build_bank(), ensure_ascii=False, indent=2))
