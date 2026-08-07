#!/usr/bin/env python3
"""Turn DIGEST.md into a Buttondown API payload.

Kept as a file rather than an inline heredoc: the previous version redirected
the script's stdout into payload.json while also writing that file, so its own
progress messages corrupted the JSON.
"""
import datetime, json, os, sys

body = open("DIGEST.md", encoding="utf-8").read()
lines = body.split("\n")

# the file opens with its own H1; Buttondown already shows the subject
if lines and lines[0].startswith("#"):
    subject = lines[0].lstrip("# ").strip()
    body = "\n".join(lines[1:]).lstrip("\n")
else:
    subject = f"Crosstalk — week ending {datetime.date.today():%-d %B %Y}"

status = "about_to_send" if os.environ.get("MODE") == "send" else "draft"
json.dump({"subject": subject, "body": body, "status": status},
          open("payload.json", "w", encoding="utf-8"))

print(f"subject: {subject}")
print(f"status:  {status}")
print(f"body:    {len(body)} characters")
if len(body) < 200:
    print("::warning::the digest is very short — check the collector ran")
