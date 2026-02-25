import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sync_manager import SyncEvent, SyncEventType, SyncManager


class FailingWebSocket:
    async def send_text(self, _message: str) -> None:
        raise RuntimeError("socket closed")


def test_broadcast_to_chat_removes_failed_connection_from_user_map():
    async def run() -> None:
        manager = SyncManager()
        chat_key = manager.make_chat_key("alice", "proj", "chat")

        conn_id = await manager.register_connection(chat_key, "alice", FailingWebSocket())
        event = SyncEvent(type=SyncEventType.STATE_UPDATE, data={"ok": True})

        sent = await manager.broadcast_to_chat(chat_key, event)

        assert sent == 0
        assert chat_key not in manager._connections or conn_id not in manager._connections.get(chat_key, {})
        assert "alice" not in manager._user_connections or conn_id not in manager._user_connections.get("alice", {})

    asyncio.run(run())
