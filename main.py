import argparse
import configparser
import sqlite3
import os
from urllib.parse import urlparse
import telnetlib
import traceback
from urllib.request import urlopen
from email.message import EmailMessage
import smtplib
import time
import uuid
from datetime import datetime, timedelta
from urllib.error import HTTPError

class MonitoringException(Exception):
    pass

def get_config():
    dir_path = os.path.dirname(os.path.realpath(__file__))

    parser = configparser.ConfigParser()
    parser.read(os.path.join(dir_path, "monitoring.cfg"))

    assert parser["main"]["smtp_host"]
    assert parser["main"]["stmp_user"]
    assert parser["main"]["smtp_password"]
    assert parser["main"]["smtp_from"]

    smtp_host = parser["main"]["smtp_host"]
    stmp_user = parser["main"]["stmp_user"]
    smtp_password = parser["main"]["smtp_password"]
    starttls =  parser["main"].getboolean("starttls", fallback=True)
    smtp_port = parser["main"].getint("port", fallback=587)
    smtp_from = parser["main"]["smtp_from"]
    smtp_to = parser["main"]["smtp_to"]

    smtp = smtplib.SMTP(host=smtp_host, port=smtp_port)
    if starttls:
        smtp.starttls()

    smtp.login(user=stmp_user, password=smtp_password)
    
    urls = parser["monitoring"]["urls"].strip().replace("\n","").split(",")

    logdb = sqlite3.connect(os.path.join(dir_path, "monitoring.db"))

    migration = """
        create table if not exists events (
            id text,
            ts bigint,
            description text
        )
    """
    cursor = logdb.cursor()
    cursor.execute(migration)
    logdb.commit()

    return {
        "smtp": smtp,
        "urls": urls,
        "logdb": logdb,
        "from": smtp_from,
        "to": smtp_to
    }

def find_url_type(url):
    parsed = urlparse(url)
    if parsed.scheme in ("http", "https"):
        return "urllib"
    elif parsed.path.isdigit():
        return "telnet"
    else:
        raise MonitoringException(f"cannot determine type: {url}")

def telnet(url):
    try:
        host, port = url.split(":")
        tn = telnetlib.Telnet()
        tn.open(host=host, port=int(port), timeout=10)
        tn.close()
        return True, ""
    except Exception:
        return False, traceback.format_exc()

def request(url):
    try:
        with urlopen(url) as response:
            _ = response.read()
            return True, ""
    except HTTPError as error:
        if error.status >= 500:
            return False, error.reason
        else:
            return True, ""
    except Exception:
        return False, traceback.format_exc()

def run_url(url):
    url_type = find_url_type(url)
    if url_type == "urllib":
        status, context = request(url)
    elif url_type == "telnet":
        status, context = telnet(url)
    else:
        raise MonitoringException(f"cannot determine type: {url}")

    return status, context

def send_notification(smtp, subject, message, from_email, to_email):
    msg = EmailMessage()
    msg.set_content(message)
    msg["Subject"] = subject
    msg["From"] = from_email
    msg["To"] = to_email
    smtp.send_message(msg)

def build_message(url, context):
    return f"""
    Job check for {url} failed.

    Exception:
    {context}

    """

def log_event(db, event):
    cursor = db.cursor()
    cursor.execute("insert into events (id, ts, description) values (?, ?, ?)", (str(uuid.uuid4()), int(time.time()), event))
    db.commit()

def query_event_count(db, event, start, end):
    cursor = db.cursor()
    response = cursor.execute("select count(*) c from events where description = ? and ts >= ? and ts <= ?", (event, int(start.timestamp()), int(end.timestamp())))
    return int(response.fetchone()[0])
 

def run_regular(config):
    urls = config["urls"]
    smtp = config["smtp"]
    from_email = config["from"]
    to_email = config["to"]
    db = config["logdb"]

    for url in urls:
        status, context = run_url(url)

        if not status:
            send_notification(smtp, f"[URGENT] Service failed for url {url}", build_message(url, context), from_email, to_email)
            log_event(db, f"failed {url}")
        else:
            log_event(db, f"success {url}")

def run_resume(config):
    urls = config["urls"]
    smtp = config["smtp"]
    from_email = config["from"]
    to_email = config["to"]
    db = config["logdb"]

    today = datetime.now()
    yesterday = today - timedelta(hours=24)

    url_status_list = []
    for url in urls:
        failed_count = query_event_count(db, f"failed {url}", yesterday, today)
        success_count = query_event_count(db, f"success {url}", yesterday, today)
        url_status_list.append((url, failed_count, success_count))

    total_errors = sum([x[1] for x in url_status_list])
    total_runs = total_errors + sum([x[2] for x in url_status_list])

    per_url_message = '\t' + '\n\t'.join([f'{x[0]}: {x[1]} / {x[2] + x[1]}' for x in url_status_list])

    message = f"""
    Here is your summary from {yesterday.isoformat().split('.')[0]} until {today.isoformat().split('.')[0]}:

    Total Runs: {total_runs}
    Total Errors:  {total_errors}
    
    Per URL (url: errors / runs):
    {per_url_message}

    Have a great day!
    """

    send_notification(smtp, "Monitoring Daily Resume", message, from_email, to_email)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--type", choices=["regular", "resume"], required=True)
    args = parser.parse_args()
    run_type = args.type

    config = get_config()

    if run_type == "regular":
        run_regular(config)
    elif run_type == "resume":
        run_resume(config)
    else:
        raise MonitoringException("cannot find run type")
