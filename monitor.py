import argparse
import json
import os
import smtplib
import sqlite3
from dataclasses import dataclass
from decimal import Decimal
from email.message import EmailMessage
from pathlib import Path
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
from dotenv import load_dotenv

START = "https://books.toscrape.com/"

@dataclass(frozen=True)
class Book:
    url: str
    title: str
    pence: int

def parse(html, url):
    page = BeautifulSoup(html, "html.parser")
    books = []
    for card in page.select("article.product_pod"):
        link = card.select_one("h3 a")
        price = card.select_one(".price_color").get_text(strip=True)
        books.append(Book(urljoin(url, link["href"]), link["title"], int(Decimal(price.removeprefix("£")) * 100)))
    next_page = page.select_one(".next a")
    return books, urljoin(url, next_page["href"]) if next_page else None

def scan(limit):
    from playwright.sync_api import sync_playwright

    url = START
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch()
        tab = browser.new_page()
        for _ in range(limit):
            if not url:
                break
            response = tab.goto(url, wait_until="domcontentloaded", timeout=30000)
            if not response or not response.ok:
                raise RuntimeError("The demo catalogue returned an HTTP error")
            books, url = parse(tab.content(), tab.url)
            yield from books
        browser.close()

def process(books, db, keyword, max_pence):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS history (
            id INTEGER PRIMARY KEY, url TEXT NOT NULL, title TEXT NOT NULL,
            pence INTEGER NOT NULL, seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS history_url ON history(url, id);
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, message TEXT NOT NULL,
            email_sent INTEGER DEFAULT 0, telegram_sent INTEGER DEFAULT 0
        );
    """)
    with db:
        for book in books:
            previous = db.execute("SELECT pence FROM history WHERE url=? ORDER BY id DESC LIMIT 1", (book.url,)).fetchone()
            if previous and previous[0] == book.pence:
                continue
            db.execute("INSERT INTO history(url,title,pence) VALUES(?,?,?)", (book.url, book.title, book.pence))
            if keyword.casefold() in book.title.casefold() and book.pence <= max_pence and (not previous or book.pence < previous[0]):
                message = f"{book.title} — £{book.pence / 100:.2f}\n{book.url}"
                db.execute("INSERT INTO outbox(message) VALUES(?)", (message,))

def telegram(message):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    body = json.dumps({"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": message}).encode()
    req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=body, headers={"Content-Type":"application/json"})
    try:
        with urlopen(req, timeout=15) as response:
            if not json.load(response)["ok"]:
                raise RuntimeError("Telegram rejected the notification")
    except Exception:
        raise RuntimeError("Telegram notification failed") from None

def email(message):
    mail = EmailMessage()
    mail["Subject"] = "Price Watch | dopasowanie"
    mail["From"] = os.environ["SMTP_FROM"]
    mail["To"] = os.environ["SMTP_TO"]
    mail.set_content(message)
    with smtplib.SMTP_SSL(os.environ["SMTP_HOST"], int(os.getenv("SMTP_PORT", "465")), timeout=15) as client:
        if os.getenv("SMTP_USER"):
            client.login(os.environ["SMTP_USER"], os.environ["SMTP_PASSWORD"])
        client.send_message(mail)

def deliver(db, dry_run):
    for ident, message, email_sent, telegram_sent in db.execute("SELECT * FROM outbox ORDER BY id").fetchall():
        if dry_run:
            print(message)
            continue
        if os.getenv("SMTP_HOST") and not email_sent:
            email(message)
            with db:
                db.execute("UPDATE outbox SET email_sent=1 WHERE id=?", (ident,))
        if os.getenv("TELEGRAM_BOT_TOKEN") and not telegram_sent:
            telegram(message)
            with db:
                db.execute("UPDATE outbox SET telegram_sent=1 WHERE id=?", (ident,))

def main():
    load_dotenv()
    cli = argparse.ArgumentParser()
    cli.add_argument("--fixture", type=Path, help="Parse a local HTML fixture")
    cli.add_argument("--dry-run", action="store_true", help="Print pending matches without sending")
    opts = cli.parse_args()
    source = parse(opts.fixture.read_text(), START)[0] if opts.fixture else scan(int(os.getenv("MAX_PAGES", "2")))
    with sqlite3.connect(os.getenv("DB_PATH", "monitor.sqlite3")) as db:
        process(source, db, os.getenv("KEYWORD", ""), int(Decimal(os.getenv("MAX_PRICE_GBP", "25")) * 100))
        deliver(db, opts.dry_run)

if __name__ == "__main__":
    main()
