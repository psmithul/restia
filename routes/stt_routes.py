# routes/stt_routes.py
"""STT API routes — multi-provider (local Whisper, API endpoint, browser)."""

import hashlib
import logging

from fastapi import APIRouter, HTTPException, Request, UploadFile, File

from src.auth_helpers import effective_user, resolved_runtime_owner
from src.life_ingestion import LifeIngestionError, ingest_inbox_capture
from src.upload_limits import read_upload_limited, STT_MAX_AUDIO_BYTES

logger = logging.getLogger(__name__)


def setup_stt_routes(stt_service):
    """Setup STT routes with the provided STT service"""
    router = APIRouter(prefix="/api/stt", tags=["stt"])

    @router.get("/stats")
    async def get_stt_stats():
        """Get STT service statistics"""
        try:
            return stt_service.get_stats()
        except Exception as e:
            logger.error(f"Failed to get STT stats: {e}")
            raise HTTPException(status_code=500, detail=str(e))

    @router.post("/transcribe")
    async def transcribe_audio(request: Request, file: UploadFile = File(...)):
        """Transcribe uploaded audio file to text"""
        try:
            if not stt_service.available:
                raise HTTPException(
                    status_code=503,
                    detail={"message": "STT service not available or set to browser mode"}
                )

            audio_bytes = await read_upload_limited(file, STT_MAX_AUDIO_BYTES, "Audio file")
            if not audio_bytes:
                raise HTTPException(status_code=400, detail={"message": "Empty audio file"})

            text = stt_service.transcribe(audio_bytes)
            if text is None:
                raise HTTPException(
                    status_code=500,
                    detail={"message": "Transcription failed"}
                )

            app_state = getattr(getattr(request, "app", None), "state", None)
            auth_mgr = getattr(app_state, "auth_manager", None)
            current_user = effective_user(request)
            if bool(auth_mgr and auth_mgr.is_configured) and not current_user:
                raise HTTPException(status_code=403, detail="Access denied")
            owner = resolved_runtime_owner(current_user)
            audio_sha256 = hashlib.sha256(audio_bytes).hexdigest()
            try:
                capture = ingest_inbox_capture(
                    owner=owner,
                    source_type="voice",
                    title=(str(file.filename or "Voice note")[:240]),
                    content=str(text),
                    source_ref=f"voice-sha256:{audio_sha256}",
                    metadata={
                        "voice": {
                            "audio_sha256": audio_sha256,
                            "mime": str(file.content_type or "audio/webm")[:200],
                            "byte_length": len(audio_bytes),
                            "transcribed": True,
                        }
                    },
                    idempotency_key=f"voice-capture:{audio_sha256}",
                    audit_interface="voice",
                )
            except LifeIngestionError as exc:
                logger.error(
                    "Voice transcription completed but Inbox ingestion failed",
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=500,
                    detail={
                        "message": (
                            "Transcription completed but Universal Inbox "
                            "ingestion failed; retry safely"
                        )
                    },
                ) from exc

            return {
                "text": text,
                "inbox_item_id": capture.inbox_id,
                "capture_created": capture.created,
                "capture_source_type": capture.source_type,
            }

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Transcription error: {e}", exc_info=True)
            raise HTTPException(
                status_code=500,
                detail={"message": f"Transcription failed: {str(e)}"}
            )

    return router
