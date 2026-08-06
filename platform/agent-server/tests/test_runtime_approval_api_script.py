def test_api_recovery_script_is_present():
    from pathlib import Path
    assert Path("scripts/runtime_approval_api_recovery.py").exists()
