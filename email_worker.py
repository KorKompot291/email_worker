import asyncio
import asyncpg
import imaplib
import smtplib
import email
import os
import re
import json
import ssl
import socket
from email.mime.text import MIMEText
from email.utils import parseaddr, make_msgid
from dotenv import load_dotenv

load_dotenv()

# ===================== NETWORK HELPERS =====================
def _resolve_ipv4(host: str) -> str:
    """Return an IPv4 address for host. Helps on platforms without IPv6 egress (e.g. some PaaS)."""
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET, type=socket.SOCK_STREAM)
        if infos:
            return infos[0][4][0]
    except Exception:
        pass
    return host  # fallback: let the OS resolve

def _smtp_connect_starttls(host: str, port: int, timeout: int = 30) -> smtplib.SMTP:
    """SMTP connect via IPv4 and upgrade to TLS (STARTTLS)."""
    ip = _resolve_ipv4(host)
    server = smtplib.SMTP(timeout=timeout)
    server._host = host  # used as SNI hostname by smtplib during starttls
    server.connect(ip, port)
    server.ehlo()
    ctx = ssl.create_default_context()
    server.starttls(context=ctx)
    server.ehlo()
    return server

def _imap_connect_ssl(host: str, port: int, timeout: int = 30) -> imaplib.IMAP4_SSL:
    """IMAP SSL connect via IPv4. We disable hostname check because we connect by IP."""
    ip = _resolve_ipv4(host)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    # keep certificate verification
    ctx.verify_mode = ssl.CERT_REQUIRED
    return imaplib.IMAP4_SSL(host=ip, port=port, ssl_context=ctx, timeout=timeout)

# ===================== CONFIG =====================

DB_DSN = os.getenv("DB_DSN")

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
IMAP_PORT = int(os.getenv("IMAP_PORT", "993"))
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))

POLL_SECONDS = int(os.getenv("POLL_SECONDS", "30"))
OUTREACH_BATCH = int(os.getenv("OUTREACH_BATCH", "10"))

INTRO_SUBJECT = "Khmer Soul Tours — cooperation / សហការ"

INTRO_TEXT = """Hello!

We are Khmer Soul Tours.
We are collecting information from local service providers so we can cooperate and bring you clients.

Your answers will be saved and sent to our manager for moderation.

If you agree to participate, please reply:
YES — to start
NO — to stop

You can also reply:
LANG EN — English
LANG KM — Khmer

––––––––––––––––

សួស្តី!

យើងជាក្រុម Khmer Soul Tours។
យើងកំពុងប្រមូលព័ត៌មានពីអ្នកផ្តល់សេវាកម្មក្នុងតំបន់ ដើម្បីអាចសហការនិងនាំអតិថិជនមកអ្នក។

ចម្លើយរបស់អ្នកនឹងត្រូវបានរក្សាទុក ហើយផ្ញើទៅអ្នកគ្រប់គ្រងរបស់យើងសម្រាប់ការត្រួតពិនិត្យ។

បើអ្នកយល់ព្រម សូមឆ្លើយតប៖
YES — ដើម្បីចាប់ផ្តើម
NO — ដើម្បីបញ្ឈប់

LANG EN / LANG KM
"""

TEXTS = {
    "confirm_start": {
        "en": "Great! Let's start.\n\nPlease choose your service type (reply with a number):\n\n{choices}",
        "km": "ល្អណាស់! សូមជ្រើសប្រភេទសេវាកម្ម (ឆ្លើយជាលេខ):\n\n{choices}",
    },
    "bad_choice": {
        "en": "Please reply with a number from the list (for example: 1 or 2).",
        "km": "សូមឆ្លើយជាលេខពីបញ្ជី (ឧទាហរណ៍៖ 1 ឬ 2)។",
    },
    "ask_contact": {
        "en": "How should we address you? (optional)\nPlease write your name + preferred title (Mr/Mrs/Ms), for example: \"Mr Sokha\" or \"Sokha (Ms)\".\n\nYou can also type 'skip'.",
        "km": "យើងគួរហៅអ្នកដូចម្តេច? (មិនចាំបាច់)\nសូមសរសេរឈ្មោះ + ការហៅ (Mr/Mrs/Ms) ឧទាហរណ៍៖ \"Mr Sokha\" ឬ \"Sokha (Ms)\"។\n\nអ្នកអាចសរសេរ 'skip' បានផងដែរ។",
    },

    # ✅ вместо "gender" — один вопрос про обращение (title)    "lang_set": {"en": "Language updated ✅", "km": "បានប្ដូរភាសារួចរាល់ ✅"},
    "stopped": {
        "en": "No problem. If you change your mind, just email us again anytime.",
        "km": "មិនអីទេ។ បើអ្នកចង់ចាប់ផ្តើមម្ដងទៀត សូមផ្ញើអ៊ីមែលមកយើងពេលណាក៏បាន។",
    },
    "done": {
        "en": "Thanks! Your details were received and sent for moderation. We'll get back to you soon.\n\n@khmersoultours",
        "km": "អរគុណ! ព័ត៌មានរបស់អ្នកត្រូវបានទទួល និងផ្ញើសម្រាប់ការត្រួតពិនិត្យ។ យើងនឹងតបត្រឡប់ឆាប់ៗនេះ។\n\n@khmersoultours",
    },
    "skip_ok": {
        "en": "Okay ✅",
        "km": "បានហើយ ✅",
    }
}

# ===================== LOGGING =====================

def log(*args):
    print("[email_worker]", *args)

# ===================== HELPERS =====================

def clean_reply_text(raw: str) -> str:
    lines = (raw or "").splitlines()
    out = []
    for l in lines:
        s = l.strip()
        if not s:
            continue
        if s.startswith(">"):
            continue
        if re.search(r"\bwrote:\b", s.lower()):
            break
        if s == "--":
            break
        out.append(s)
    return " ".join(out).strip()


def norm_cmd(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def parse_first_number(text: str):
    m = re.search(r"\b(\d{1,2})\b", text or "")
    return int(m.group(1)) if m else None


def as_dict_state(state_value):
    if state_value is None:
        return {}
    if isinstance(state_value, dict):
        return state_value
    if isinstance(state_value, str):
        s = state_value.strip()
        if not s:
            return {}
        try:
            return json.loads(s)
        except Exception:
            return {}
    return {}


def is_yes(text: str) -> bool:
    # ✅ YES/Yes/yES/yes!/ok yes/yep/yeah/y
    c = norm_cmd(text)
    return bool(re.search(r"\b(yes|y|ok|okay|yeah|yep)\b", c))


def is_no(text: str) -> bool:
    c = norm_cmd(text)
    return bool(re.search(r"\b(no|n|stop|cancel)\b", c))


def normalize_lang(lang: str | None) -> str:
    lang = (lang or "").strip().lower()
    if lang in ("km", "kh", "khmer"):
        return "km"
    return "en"


def _merge_references(existing_refs: str | None, new_msgid: str | None) -> str:
    # Gmail threading: References = "msgid1 msgid2 ..."
    refs = []
    if existing_refs:
        refs.extend([r.strip() for r in existing_refs.split() if r.strip()])
    if new_msgid and new_msgid.strip():
        if new_msgid.strip() not in refs:
            refs.append(new_msgid.strip())
    return " ".join(refs).strip()


def send_email(to_email: str, subject: str, body: str, *, in_reply_to: str | None = None, references: str | None = None):
    """Send plain text email via Gmail SMTP (STARTTLS).

    On some PaaS networks IPv6 egress can be blocked which makes smtp.gmail.com fail with
    "Network is unreachable". We force IPv4 resolution and retry a few times.
    """
    msg = MIMEText(body, "plain", "utf-8")
    msg["From"] = GMAIL_ADDRESS
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid()

    # ✅ keep the conversation in one thread
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references

    last_err: Exception | None = None
    for attempt in range(1, 4):
        try:
            server = _smtp_connect_starttls(SMTP_HOST, SMTP_PORT, timeout=30)
            try:
                server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
                server.send_message(msg)
                return
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
        except Exception as e:
            last_err = e
            # short backoff
            time.sleep(1.5 * attempt)

    raise last_err or RuntimeError("SMTP send failed")

async def ensure_provider_by_email(conn, email_addr: str) -> str:
    email_addr = (email_addr or "").strip().lower()

    row = await conn.fetchrow("""
        select id from supplier.providers
        where lower(email)=lower($1)
        limit 1
    """, email_addr)
    if row:
        return str(row["id"])

    try:
        await conn.execute("""
            insert into supplier.providers (email, preferred_lang)
            values ($1, 'en')
        """, email_addr)
    except asyncpg.exceptions.UniqueViolationError:
        pass

    row = await conn.fetchrow("""
        select id from supplier.providers
        where lower(email)=lower($1)
        limit 1
    """, email_addr)
    if not row:
        raise RuntimeError("Provider insert/select failed for email=" + email_addr)
    return str(row["id"])


async def get_active_email_session(conn, provider_id: str):
    # ✅ фикс опечатки статуса
    return await conn.fetchrow("""
        select id, state, current_question_id, category_id, status
        from supplier.intake_sessions
        where provider_id=$1
          and channel='email'
          and status=$2
        order by started_at desc nulls last
        limit 1
    """, provider_id, SESSION_STATUS_IN_PROGRESS)


async def create_email_session(conn, provider_id: str, lang: str, thread_msgid: str | None, thread_refs: str | None) -> str:
    state = {"lang": lang, "step": "await_yes"}
    if thread_msgid:
        state["thread_in_reply_to"] = thread_msgid
    if thread_refs:
        state["thread_references"] = thread_refs

    row = await conn.fetchrow("""
        insert into supplier.intake_sessions
        (provider_id, channel, status, started_at, state)
        values ($1, 'email', $2, now(), $3::jsonb)
        returning id
    """, provider_id, SESSION_STATUS_IN_PROGRESS, json.dumps(state))
    return str(row["id"])


async def update_state(conn, session_id: str, patch: dict):
    await conn.execute("""
        update supplier.intake_sessions
        set state = coalesce(state,'{}'::jsonb) || $2::jsonb
        where id=$1
    """, session_id, json.dumps(patch))


async def set_session_category(conn, session_id: str, category_id: str):
    await conn.execute("""
        update supplier.intake_sessions
        set category_id=$2
        where id=$1
    """, session_id, category_id)


async def set_current_question(conn, session_id: str, question_id: str | None):
    await conn.execute("""
        update supplier.intake_sessions
        set current_question_id=$2
        where id=$1
    """, session_id, question_id)


async def complete_session(conn, session_id: str):
    await conn.execute("""
        update supplier.intake_sessions
        set status='completed', finished_at=now()
        where id=$1
    """, session_id)


async def get_categories(conn):
    return await conn.fetch("""
        select id, code, title_en, title_km
        from supplier.service_categories
        where is_active=true
        order by code
    """)


def format_categories(rows, lang: str):
    out = []
    for i, r in enumerate(rows, start=1):
        title = r["title_en"] if lang == "en" else (r["title_km"] or r["title_en"])
        out.append(f"{i}) {title}")
    return "\n".join(out)


async def get_next_question(conn, session_id: str, category_id: str):
    return await conn.fetchrow("""
        select q.id, q.text_en, q.text_km, q.answer_type, q.options, q.validation
        from supplier.questions q
        left join supplier.answers a
               on a.session_id=$1 and a.question_id=q.id
        where q.category_id=$2
          and q.is_active=true
          and q.code not in ('gender','title','salutation','name','contact','contact_name','contact_title','preferred_title','preferred_name')
          and a.id is null
        order by q.sort_order asc, q.created_at asc
        limit 1
    """, session_id, category_id)


async def save_answer_text(conn, session_id: str, question_id: str, text_original: str):
    await conn.execute("""
        insert into supplier.answers
            (session_id, question_id, answer_raw, answer_text_original, answer_text_ru, translation_meta)
        values
            ($1, $2, $3::jsonb, $4, $5, '{}'::jsonb)
        on conflict (session_id, question_id)
        do update set
            answer_raw=excluded.answer_raw,
            answer_text_original=excluded.answer_text_original,
            answer_text_ru=excluded.answer_text_ru,
            updated_at=now()
    """, session_id, question_id, json.dumps({"text": text_original}), text_original, text_original)


# -------- Outreach queue --------

async def fetch_pending_outreach(conn, limit: int = 10):
    return await conn.fetch("""
        select id, email, preferred_lang
        from supplier.email_outreach_queue
        where status in ('queued','pending')
        order by id
        limit $1
        for update skip locked
    """, limit)


async def mark_outreach_sent(conn, outreach_id: str, provider_id: str | None):
    try:
        await conn.execute("""
            update supplier.email_outreach_queue
            set status='sent', sent_at=now(), provider_id=$2
            where id=$1
        """, outreach_id, provider_id)
    except Exception:
        await conn.execute("""
            update supplier.email_outreach_queue
            set status='sent'
            where id=$1
        """, outreach_id)


async def mark_outreach_bad(conn, outreach_id: str, status: str = "bad_email"):
    try:
        await conn.execute("""
            update supplier.email_outreach_queue
            set status=$2, last_error=$3
            where id=$1
        """, outreach_id, status, status)
    except Exception:
        await conn.execute("""
            update supplier.email_outreach_queue
            set status=$2
            where id=$1
        """, outreach_id, status)


async def process_outreach_queue(conn):
    rows = await fetch_pending_outreach(conn, limit=OUTREACH_BATCH)
    if not rows:
        return

    for r in rows:
        outreach_id = str(r["id"])
        email_addr = (r["email"] or "").strip().lower()
        lang = normalize_lang(r.get("preferred_lang"))

        if not email_addr or "@" not in email_addr:
            await mark_outreach_bad(conn, outreach_id, "bad_email")
            continue

        try:
            provider_id = await ensure_provider_by_email(conn, email_addr)
            send_email(email_addr, INTRO_SUBJECT, INTRO_TEXT)
            await mark_outreach_sent(conn, outreach_id, provider_id)
            log("Outreach intro sent to", email_addr, "lang=", lang)
        except Exception as e:
            log("Outreach error for", email_addr, ":", e)
            await mark_outreach_bad(conn, outreach_id, "send_failed")


# ===================== IMAP PARSING =====================

def extract_text_body(msg: email.message.Message) -> str:
    if msg.is_multipart():
        parts = []
        for part in msg.walk():
            ctype = (part.get_content_type() or "").lower()
            disp = (part.get("Content-Disposition") or "").lower()
            if "attachment" in disp:
                continue
            if ctype in ("text/plain", "text/html"):
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                try:
                    text = payload.decode(charset, errors="ignore")
                except Exception:
                    text = payload.decode("utf-8", errors="ignore")
                parts.append((ctype, text))

        for ctype, text in parts:
            if ctype == "text/plain" and text.strip():
                return text

        for ctype, text in parts:
            if ctype == "text/html" and text.strip():
                text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
                text = re.sub(r"(?is)<br\s*/?>", "\n", text)
                text = re.sub(r"(?is)</p\s*>", "\n", text)
                text = re.sub(r"(?is)<.*?>", " ", text)
                text = re.sub(r"[ \t]+", " ", text)
                return text
        return ""
    else:
        payload = msg.get_payload(decode=True)
        if payload is None:
            raw = msg.get_payload()
            return raw if isinstance(raw, str) else ""
        charset = msg.get_content_charset() or "utf-8"
        try:
            return payload.decode(charset, errors="ignore")
        except Exception:
            return payload.decode("utf-8", errors="ignore")


# ===================== EMAIL FLOW =====================

def _get_thread_headers_from_state(st: dict, inbound_msgid: str | None, inbound_refs: str | None):
    # приоритет: то, что пришло сейчас → иначе то, что запомнили
    in_reply_to = inbound_msgid or st.get("thread_in_reply_to")
    references = st.get("thread_references") or inbound_refs
    # пополняем references текущим msgid
    references = _merge_references(references, inbound_msgid)
    return in_reply_to, references


async def send_categories(conn, to_email: str, lang: str, *, in_reply_to: str | None = None, references: str | None = None):
    cats = await get_categories(conn)
    body = TEXTS["confirm_start"][lang].format(choices=format_categories(cats, lang))
    send_email(to_email, INTRO_SUBJECT, body, in_reply_to=in_reply_to, references=references)
    log("Sent categories to", to_email)


async def send_next_question_or_finish(conn, to_email: str, session_id: str, lang: str, *, in_reply_to: str | None = None, references: str | None = None):
    session = await conn.fetchrow("""
        select category_id
        from supplier.intake_sessions
        where id=$1
    """, session_id)
    category_id = session["category_id"]
    if not category_id:
        await send_categories(conn, to_email, lang, in_reply_to=in_reply_to, references=references)
        await update_state(conn, session_id, {"step": "await_category"})
        return

    q = await get_next_question(conn, session_id, str(category_id))
    if not q:
        await complete_session(conn, session_id)
        send_email(to_email, INTRO_SUBJECT, TEXTS["done"][lang], in_reply_to=in_reply_to, references=references)
        log("Session completed:", session_id, "email:", to_email)
        return

    await set_current_question(conn, session_id, str(q["id"]))
    text_q = q["text_en"] if lang == "en" else (q["text_km"] or q["text_en"])
    send_email(to_email, INTRO_SUBJECT, text_q, in_reply_to=in_reply_to, references=references)
    log("Asked question", str(q["id"]), "to", to_email)


async def handle_incoming(conn, from_email: str, raw_text: str, inbound_msgid: str | None, inbound_refs: str | None):
    text = clean_reply_text(raw_text)
    cmd = norm_cmd(text)

    if not cmd:
        return

    log("INCOMING from:", from_email, "|", cmd[:160])

    provider_id = await ensure_provider_by_email(conn, from_email)
    session = await get_active_email_session(conn, provider_id)

    # language command
    if cmd.startswith("lang"):
        lang = "km" if "km" in cmd else "en"
        if session:
            st = as_dict_state(session["state"])
            in_reply_to, references = _get_thread_headers_from_state(st, inbound_msgid, inbound_refs)
            await update_state(conn, str(session["id"]), {"lang": lang, "thread_in_reply_to": in_reply_to, "thread_references": references})
        else:
            in_reply_to, references = inbound_msgid, _merge_references(inbound_refs, inbound_msgid)
        send_email(from_email, INTRO_SUBJECT, TEXTS["lang_set"][lang], in_reply_to=in_reply_to, references=references)
        return

    # stop
    if is_no(cmd):
        in_reply_to = inbound_msgid
        references = _merge_references(inbound_refs, inbound_msgid)
        send_email(from_email, INTRO_SUBJECT, TEXTS["stopped"]["en"], in_reply_to=in_reply_to, references=references)
        return

    # start (YES variants)
    if is_yes(cmd):
        # если сессии нет — создаём
        if not session:
            lang = "en"
            refs = _merge_references(inbound_refs, inbound_msgid)
            session_id = await create_email_session(conn, provider_id, lang, inbound_msgid, refs)
            st = {"lang": lang, "step": "await_yes", "thread_in_reply_to": inbound_msgid, "thread_references": refs}
        else:
            session_id = str(session["id"])
            st = as_dict_state(session["state"])
            lang = normalize_lang(st.get("lang", "en"))

        in_reply_to, references = _get_thread_headers_from_state(st, inbound_msgid, inbound_refs)

        # ✅ после YES сразу просим выбрать сферу (без "sorry")
        await update_state(conn, session_id, {"lang": lang, "step": "await_category", "thread_in_reply_to": in_reply_to, "thread_references": references})
        await send_categories(conn, from_email, lang, in_reply_to=in_reply_to, references=references)
        return

    # If no active session — send intro
    if not session:
        in_reply_to = inbound_msgid
        references = _merge_references(inbound_refs, inbound_msgid)
        send_email(from_email, INTRO_SUBJECT, INTRO_TEXT, in_reply_to=in_reply_to, references=references)
        return

    session_id = str(session["id"])
    st = as_dict_state(session["state"])
    lang = normalize_lang(st.get("lang", "en"))
    step = st.get("step")

    in_reply_to, references = _get_thread_headers_from_state(st, inbound_msgid, inbound_refs)
    # обновляем в состоянии, чтобы держать thread даже если клиент шлёт без refs
    await update_state(conn, session_id, {"thread_in_reply_to": in_reply_to, "thread_references": references})

    # Step: await_category
    if step == "await_category":
        cats = await get_categories(conn)
        n = parse_first_number(cmd)

        if not n or n < 1 or n > len(cats):
            send_email(
                from_email,
                INTRO_SUBJECT,
                TEXTS["bad_choice"][lang] + "\n\n" + TEXTS["confirm_start"][lang].format(choices=format_categories(cats, lang)),
                in_reply_to=in_reply_to,
                references=references
            )
            return

        chosen = cats[n - 1]
        await set_session_category(conn, session_id, str(chosen["id"]))

        # ✅ дальше: один вопрос про обращение (Mr/Mrs/Ms)
        await update_state(conn, session_id, {"step": "await_contact"})
        send_email(from_email, INTRO_SUBJECT, TEXTS["ask_contact"][lang], in_reply_to=in_reply_to, references=references)
        log("Category chosen:", chosen["id"], "email:", from_email)
        return

    # Step: await_contact (optional)
    if step == "await_contact":
        raw = (text or "").strip()
        if raw.lower() in ("skip", "pass", "-"):
            await update_state(conn, session_id, {"contact": None, "step": "await_question"})
            await send_next_question_or_finish(conn, from_email, session_id, lang, in_reply_to=in_reply_to, references=references)
            return

        if raw:
            # store everything in ONE field (as requested)
            await update_state(conn, session_id, {"contact": raw, "step": "await_question"})
            await send_next_question_or_finish(conn, from_email, session_id, lang, in_reply_to=in_reply_to, references=references)
            log("Contact saved:", raw, "email:", from_email)
            return

        send_email(from_email, INTRO_SUBJECT, TEXTS["ask_contact"][lang], in_reply_to=in_reply_to, references=references)
        return

    # Step: await_question

    # Step: await_question
    if step == "await_question":
        qid = session["current_question_id"]
        if not qid:
            await send_next_question_or_finish(conn, from_email, session_id, lang, in_reply_to=in_reply_to, references=references)
            return

        await save_answer_text(conn, session_id, str(qid), (text or "").strip())
        log("Answer saved. qid=", str(qid), "email=", from_email)

        await send_next_question_or_finish(conn, from_email, session_id, lang, in_reply_to=in_reply_to, references=references)
        return

    # fallback
    send_email(from_email, INTRO_SUBJECT, INTRO_TEXT, in_reply_to=in_reply_to, references=references)


# ===================== IMAP LOOP =====================

async def process_incoming_emails(conn):
    mail = _imap_connect_ssl(IMAP_HOST, IMAP_PORT)
    mail.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
    mail.select("inbox")

    status, messages = mail.search(None, "UNSEEN")
    if status != "OK":
        mail.logout()
        return

    nums = messages[0].split()
    if not nums:
        mail.logout()
        return

    log("UNSEEN messages:", len(nums))

    for num in nums:
        try:
            _, data = mail.fetch(num, "(RFC822)")
            if not data or not data[0] or not data[0][1]:
                continue

            msg = email.message_from_bytes(data[0][1])
            from_email = parseaddr(msg.get("From"))[1].lower().strip()
            if not from_email or "@" not in from_email:
                continue

            inbound_msgid = (msg.get("Message-ID") or "").strip() or None
            inbound_refs = (msg.get("References") or "").strip() or None

            body = extract_text_body(msg)
            await handle_incoming(conn, from_email, body, inbound_msgid, inbound_refs)

        except Exception:
            import traceback
            log("handle_incoming error for num:", num)
            traceback.print_exc()
        finally:
            try:
                mail.store(num, "+FLAGS", "\\Seen")
            except Exception:
                pass

    mail.logout()


# ===================== MAIN =====================

async def main():
    log("Email worker started (stage3).")

    while True:
        try:
            ssl_ctx = ssl.create_default_context()
            ssl_ctx.check_hostname = False
            ssl_ctx.verify_mode = ssl.CERT_NONE

            conn = await asyncpg.connect(DB_DSN, ssl=ssl_ctx)

            await process_outreach_queue(conn)
            await process_incoming_emails(conn)

            await conn.close()

        except Exception as e:
            log("Email worker error:", e)

        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())