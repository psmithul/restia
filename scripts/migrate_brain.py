import os
import sqlite3
import logging
from mnemosyne import Mnemosyne

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def migrate():
    brain_db_path = "/Users/mika/Documents/brain/data/brain.sqlite"
    if not os.path.exists(brain_db_path):
        logger.error(f"Source database not found at {brain_db_path}")
        return

    mnemo_db_path = os.path.join(os.getcwd(), "data", "mnemosyne.db")
    logger.info(f"Connecting to mnemosyne at {mnemo_db_path}")
    mnemo = Mnemosyne(db_path=mnemo_db_path)

    conn = sqlite3.connect(brain_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, title, type, content FROM documents")
    documents = cursor.fetchall()

    logger.info(f"Found {len(documents)} documents to migrate.")

    migrated = 0
    for doc in documents:
        doc_id = doc["id"]
        title = doc["title"]
        doc_type = doc["type"]
        content = doc["content"]

        if not content.strip():
            continue

        metadata = {
            "title": title,
            "type": doc_type,
            "legacy_brain_id": doc_id,
            "source": "restia_brain"
        }

        try:
            mid = mnemo.remember(
                content=content.strip(),
                source="restia_brain",
                metadata=metadata
            )
            c2 = mnemo.conn.cursor()
            c2.execute("UPDATE working_memory SET author_id = ? WHERE id = ?", ("admin", mid))
            mnemo.conn.commit()
            migrated += 1
        except Exception as e:
            logger.error(f"Failed to migrate document {doc_id}: {e}")

    logger.info(f"Successfully migrated {migrated} documents out of {len(documents)}.")

if __name__ == "__main__":
    migrate()
