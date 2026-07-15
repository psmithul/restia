import json
from mnemosyne import Mnemosyne

m = Mnemosyne(db_path="test_mem.db")
m.remember("User likes dark mode", source="user", metadata={"category": "preference", "uses": 2})

mems = m.get_all_memories()
print(json.dumps(mems, indent=2))
