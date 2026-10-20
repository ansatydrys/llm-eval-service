"""Generate data/demo.jsonl: a synthetic dataset (fictional facts, no real-world claims)."""

import json
from pathlib import Path

EXACT = [
    ("What is 12 multiplied by 7?", "84"),
    ("What is the capital of the fictional country Veloria?", "Port Amsel"),
    ("Spell the word 'queue' backwards.", "eueuq"),
    ("How many sides does a hexagon have?", "6"),
    ("What is the next number: 2, 4, 8, 16?", "32"),
    ("Which HTTP status code means Not Found?", "404"),
    ("What is the square root of 144?", "12"),
    ("Convert 3 kilometres to metres.", "3000 metres"),
    ("What colour do you get by mixing blue and yellow?", "green"),
    ("What is the plural of 'mouse'?", "mice"),
    ("What is 100 divided by 4?", "25"),
    ("Name the default port of PostgreSQL.", "5432"),
]

FACTS = [
    ("Describe the Zentor-9 battery.",
     "The Zentor-9 battery stores 40 kWh, charges in 25 minutes and weighs 210 kg.",
     ["40 kWh", "25 minutes", "210 kg"]),
    ("Who founded the town of Brindlemoor and when?",
     "Brindlemoor was founded by Ada Quell in 1742 near the river Tamsin.",
     ["Ada Quell", "1742", "Tamsin"]),
    ("Summarize the Orvex onboarding policy.",
     "New Orvex employees get a laptop on day one, a mentor for 90 days and security training in week one.",
     ["laptop", "mentor", "90 days", "security training"]),
    ("What are the rules of the game Kettleball?",
     "Kettleball is played by two teams of five players on a round field and lasts 3 periods.",
     ["two teams", "five players", "round field", "3 periods"]),
    ("Explain how the Lumen-3 telescope is cooled.",
     "The Lumen-3 telescope is cooled with liquid helium to 4 kelvin by a closed loop cryocooler.",
     ["liquid helium", "4 kelvin", "cryocooler"]),
    ("What does the Halden refund policy say?",
     "Halden refunds unused items within 30 days if the receipt is presented and the box is sealed.",
     ["30 days", "receipt", "sealed"]),
    ("Describe the migration route of the fictional Sorrel crane.",
     "The Sorrel crane flies 6000 km from Ledra to the Ombra delta every October.",
     ["6000 km", "Ledra", "Ombra delta", "October"]),
    ("What is included in the Vantage Pro plan?",
     "Vantage Pro includes 5 seats, 200 GB storage and priority support with a 4 hour response time.",
     ["5 seats", "200 GB", "priority support", "4 hour"]),
    ("How is the Merrow bridge built?",
     "The Merrow bridge is a steel arch bridge 480 metres long opened in 1931.",
     ["steel arch", "480 metres", "1931"]),
    ("Summarize incident INC-7731.",
     "INC-7731 was a database failover that caused 12 minutes of downtime and was fixed by raising the connection limit.",
     ["database failover", "12 minutes", "connection limit"]),
    ("What are the side effects listed for the fictional drug Calmora?",
     "Calmora may cause drowsiness, dry mouth and mild headache.",
     ["drowsiness", "dry mouth", "headache"]),
    ("Describe the Pellucid file format.",
     "Pellucid files start with the magic bytes PLCD, use little endian integers and store blocks of 4096 bytes.",
     ["PLCD", "little endian", "4096 bytes"]),
    ("What did the Kessler committee recommend?",
     "The Kessler committee recommended quarterly audits, a public dashboard and an independent ombudsman.",
     ["quarterly audits", "public dashboard", "ombudsman"]),
    ("Explain the Tidewater shipping rates.",
     "Tidewater charges 4 dollars per kilogram, adds a 10 percent fuel surcharge and ships twice a week.",
     ["4 dollars per kilogram", "10 percent", "twice a week"]),
]

PERSON = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "age": {"type": "integer", "minimum": 0}},
    "required": ["name", "age"],
    "additionalProperties": False,
}
ORDER = {
    "type": "object",
    "properties": {
        "order_id": {"type": "string", "pattern": "^ORD-[0-9]+$"},
        "items": {"type": "integer", "minimum": 1},
        "express": {"type": "boolean"},
    },
    "required": ["order_id", "items", "express"],
    "additionalProperties": False,
}
JSON_CASES = [
    ("Extract the person as JSON: 'Mira Lenz is 34 years old.'", {"name": "Mira Lenz", "age": 34}, PERSON),
    ("Extract the person as JSON: 'Tobias Venn, aged 52.'", {"name": "Tobias Venn", "age": 52}, PERSON),
    ("Extract the person as JSON: 'Ines Carrow (19).'", {"name": "Ines Carrow", "age": 19}, PERSON),
    ("Extract the person as JSON: 'Our new hire Okon Dale is 41.'", {"name": "Okon Dale", "age": 41}, PERSON),
    ("Extract the person as JSON: 'Rhea Sol turned 27 today.'", {"name": "Rhea Sol", "age": 27}, PERSON),
    ("Extract the order as JSON: 'ORD-1001, 3 items, express shipping.'",
     {"order_id": "ORD-1001", "items": 3, "express": True}, ORDER),
    ("Extract the order as JSON: 'Order ORD-2042 has 1 item, standard delivery.'",
     {"order_id": "ORD-2042", "items": 1, "express": False}, ORDER),
    ("Extract the order as JSON: 'ORD-77: 12 items, express.'",
     {"order_id": "ORD-77", "items": 12, "express": True}, ORDER),
    ("Extract the order as JSON: '5 items in ORD-3150, no rush.'",
     {"order_id": "ORD-3150", "items": 5, "express": False}, ORDER),
    ("Extract the order as JSON: 'ORD-9 contains 2 items and needs express.'",
     {"order_id": "ORD-9", "items": 2, "express": True}, ORDER),
    ("Extract the person as JSON: 'Pavel Orr, 63 years.'", {"name": "Pavel Orr", "age": 63}, PERSON),
    ("Extract the order as JSON: 'ORD-4400 with 8 items, regular shipping.'",
     {"order_id": "ORD-4400", "items": 8, "express": False}, ORDER),
    ("Extract the person as JSON: 'Lina Marsh is 8 years old.'", {"name": "Lina Marsh", "age": 8}, PERSON),
    ("Extract the order as JSON: 'ORD-515: 4 items, express please.'",
     {"order_id": "ORD-515", "items": 4, "express": True}, ORDER),
]


def main() -> None:
    lines = []
    for i, (q, a) in enumerate(EXACT, 1):
        lines.append({"id": f"exact-{i:02d}", "question": q, "expected_answer": a})
    for i, (q, a, facts) in enumerate(FACTS, 1):
        lines.append({"id": f"facts-{i:02d}", "question": q, "expected_answer": a, "required_facts": facts})
    for i, (q, a, schema) in enumerate(JSON_CASES, 1):
        lines.append({"id": f"json-{i:02d}", "question": q, "expected_answer": json.dumps(a),
                      "json_schema": schema})
    out = Path(__file__).resolve().parent.parent / "data" / "demo.jsonl"
    out.write_text("".join(json.dumps(line) + "\n" for line in lines))
    print(f"wrote {len(lines)} cases to {out}")


if __name__ == "__main__":
    main()
