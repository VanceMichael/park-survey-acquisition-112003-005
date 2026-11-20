"""生态站季节档案目录及评估办公室收件台。"""
from argparse import ArgumentParser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
import json

ROOT = Path(__file__).resolve().parent


def create_source(port=0, root=ROOT):
    scenario = json.loads((root / "data/timetable.json").read_text())
    office = {item["id"]: item for item in scenario["receipts"]}
    spent = {}
    window = 0

    class Source(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, code, value, kind="application/json"):
            content = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)

        def do_GET(self):
            url = urlsplit(self.path)
            query = {key: val[0] for key, val in parse_qs(url.query).items()}
            try:
                self.get(url.path, query)
            except (KeyError, ValueError):
                self.respond(400, {"state": "invalid_query"})

        def get(self, path, query):
            if path == "/":
                return self.respond(200, {"window": window, "archives_left": scenario["allowance"] - spent.get(window, 0)})
            if path in ("/campaigns", "/cards"):
                key = path.lstrip("/")
                return self.respond(200, {key: [entry for entry in scenario[key] if entry["known"] <= window]})
            if path == "/catalog":
                station, season = query["station"], query["season"]
                editions = [item for item in scenario["catalog"] if item["station"] == station and
                            item["season"] == season and item["known"] <= window]
                if not editions:
                    return self.respond(404, {"state": "not_declared"})
                current = editions[-1]
                if query.get("edition", current["edition"]) != current["edition"]:
                    return self.respond(409, {"state": "edition_replaced", "edition": current["edition"]})
                after = int(query.get("after", 0))
                members = current["manifest"][after:after + 2]
                return self.respond(200, {**{k: v for k, v in current.items() if k != "manifest"},
                    "members": members, "next": after + 2 if len(current["manifest"]) > after + 2 else None})
            if path.startswith("/object/"):
                ident = path.rsplit("/", 1)[-1]
                item = scenario["objects"].get(ident)
                if not item or item["from"] > window:
                    return self.respond(404, {"state": "pending"})
                if item["until"] <= window:
                    return self.respond(410, {"state": "expired"})
                return self.respond(200, (root / "objects" / item["file"]).read_bytes(), "application/octet-stream")
            if path.startswith("/archive/"):
                ident = path.rsplit("/", 1)[-1]
                archive = scenario["archives"].get(ident)
                if not archive or archive["known"] > window:
                    return self.respond(404, {"state": "pending"})
                if spent.get(window, 0) >= scenario["allowance"]:
                    return self.respond(429, {"state": "archive_lane_busy"})
                spent[window] = spent.get(window, 0) + 1
                return self.respond(200, (root / "archives" / archive["file"]).read_bytes(), "application/zip")
            if path == "/office/receipts":
                return self.respond(200, {"receipts": [row for row in office.values() if row["known"] <= window]})
            self.respond(404, {"state": "unknown"})

        def do_POST(self):
            nonlocal window
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/clock":
                    if type(body["window"]) is not int or body["window"] < window:
                        return self.respond(409, {"state": "cannot_rewind"})
                    window = body["window"]
                    return self.respond(200, {"window": window})
                if self.path == "/office/submit":
                    return self.submit(body)
            except (KeyError, ValueError, TypeError, IndexError):
                return self.respond(400, {"state": "invalid_submission"})
            self.respond(404, {"state": "unknown"})

        def submit(self, body):
            key = body["receipt_request"]
            if key in office:
                previous = office[key]
                return self.respond(200 if previous.get("source") == body else 409, previous)
            results = []
            for row in body["units"]:
                identifier = row["unit"]
                if "previous" in row:
                    other = office.get(row["previous"])
                    if not other or not any(old["unit"] == identifier and old["state"] == "accepted"
                                             for old in other["units"]):
                        return self.respond(409, {"state": "no_prior_unit", "unit": identifier})
                if row["action"] == "withdraw":
                    result = {**row, "state": "withdrawn"}
                elif row["action"] == "submit":
                    required = scenario["return_missing"].get(identifier)
                    if required and required not in row["materials"]:
                        result = {**row, "state": "returned", "missing": required}
                    else:
                        result = {**row, "state": "accepted"}
                else:
                    return self.respond(422, {"state": "invalid_action"})
                results.append(result)
            value = {"id": key, "known": window, "units": results, "source": body}
            office[key] = value
            return self.respond(200, value)

    return HTTPServer(("127.0.0.1", port), Source)


if __name__ == "__main__":
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8040)
    with create_source(parser.parse_args().port) as server:
        print(f"生态保育站和评估台在 http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()
