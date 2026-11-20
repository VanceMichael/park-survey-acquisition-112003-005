import json
from hashlib import sha256
from io import BytesIO
from zipfile import ZipFile
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from source_service import create_source


class ParkTest(unittest.TestCase):
    def setUp(self):
        self.server = create_source()
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def exchange(self, path, payload=None, headers=None):
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                          headers=headers or {})
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            if response.headers["Content-Type"].startswith("application/json"):
                body = json.loads(body)
            return response.code, response.headers, body

    def advance(self, window):
        self.assertEqual(self.exchange("/clock", {"window": window})[0], 200)

    def test_archives_are_limited_and_match_members_from_a_season(self):
        catalog = self.exchange("/catalog?station=north&season=2019")[2]
        self.assertEqual(catalog["state"], "sealed")
        status, _, raw = self.exchange(catalog["archive"])
        self.assertEqual(status, 200)
        with ZipFile(BytesIO(raw)) as pack:
            for member in catalog["members"]:
                self.assertEqual(sha256(pack.read(member["member"])).hexdigest(), member["sha256"])
        self.assertEqual(self.exchange("/archive/SF19")[0], 429)
        self.advance(1)
        self.assertEqual(self.exchange("/archive/SF19")[0], 200)

    def test_open_catalog_is_replaced_and_short_term_object_disappears(self):
        self.advance(1)
        first = "/catalog?station=north&season=2026"
        source = self.exchange(first)[2]
        self.assertEqual(source["missing"], ["N26-3", "N26-4"])
        self.assertEqual(self.exchange("/object/O26-1")[0], 200)
        self.assertEqual(self.exchange("/object/P26-1")[0], 404)
        self.advance(3)
        self.assertEqual(self.exchange(first + "&edition=NU26-a&after=2")[0], 409)
        self.assertEqual(self.exchange("/object/O26-1")[0], 410)
        self.assertEqual(self.exchange("/object/P26-1")[0], 200)
        self.advance(4)
        new = self.exchange(first)[2]
        self.assertEqual(new["members"][1]["state"], "withdrawn")

    def test_office_accepts_or_returns_members_independently(self):
        self.advance(2)
        old = self.exchange("/office/receipts")[2]["receipts"][0]
        self.assertEqual([row["state"] for row in old["units"]], ["accepted", "returned"])
        self.advance(3)
        records = self.exchange("/cards")[2]["cards"]
        self.assertEqual(next(row["state"] for row in records if row["id"] == "R2a"), "review")
        submit = {"receipt_request":"ER2","units":[
            {"unit":"U-East","action":"withdraw","previous":"ER1","materials":["R2a"]},
            {"unit":"U-South","action":"submit","materials":["SP26","SO26"]},
        ]}
        self.assertEqual([row["state"] for row in self.exchange("/office/submit", submit)[2]["units"]],
                         ["withdrawn", "accepted"])
        self.assertEqual(self.exchange("/office/receipts")[2]["receipts"][0], old)
