import logging

from app.logging_config import PrivateRotatingFileHandler, configure_logging


def test_configure_logging_writes_private_rotating_file(tmp_path, monkeypatch):
    log_path = tmp_path / "runtime" / "agent-server.log"
    log_path.parent.mkdir(parents=True)
    retained_backup = log_path.parent / "agent-server.log.5"
    retained_backup.write_text("retained", encoding="utf-8")
    retained_backup.chmod(0o644)
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    monkeypatch.setenv("AGENT_LOG_FILE", str(log_path))

    try:
        configure_logging()
        logging.getLogger("agent.chat").info("incident-evidence")
        for handler in root.handlers:
            handler.flush()

        file_handler = next(
            handler
            for handler in root.handlers
            if isinstance(handler, PrivateRotatingFileHandler)
        )
        assert "incident-evidence" in log_path.read_text(encoding="utf-8")
        assert log_path.stat().st_mode & 0o777 == 0o600
        assert log_path.parent.stat().st_mode & 0o777 == 0o700
        assert retained_backup.stat().st_mode & 0o777 == 0o600

        file_handler.doRollover()
        logging.getLogger("agent.chat").info("post-rollover-evidence")
        file_handler.flush()

        assert "post-rollover-evidence" in log_path.read_text(encoding="utf-8")
        assert log_path.stat().st_mode & 0o777 == 0o600
        assert (log_path.parent / "agent-server.log.1").stat().st_mode & 0o777 == 0o600
    finally:
        for handler in root.handlers:
            if handler not in original_handlers:
                handler.close()
        root.handlers = original_handlers
        root.setLevel(original_level)