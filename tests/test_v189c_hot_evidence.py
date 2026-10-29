from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]

def test_compiles():
    for name in ('control_center/hot_evidence_audit.py','control_center/app.py','hot_evidence_audit.py'):
        ast.parse((ROOT/name).read_text(encoding='utf-8'))

def test_no_destructive_sql():
    src=(ROOT/'control_center/hot_evidence_audit.py').read_text(encoding='utf-8').lower()
    assert 'drop table processing.gambar_reused' not in src
    assert 'delete from processing.gambar_reused' not in src
    assert 'select image_bytes' not in src
    assert 'create temp table' in src
    assert 'gold.reused_photo_detail' in src
    assert 'processing.transport_kt_before_context' in src

def test_ui_manual_trigger():
    js=(ROOT/'control_center/static/control.js').read_text(encoding='utf-8')
    assert "hotAuditBtn.onclick=runHotEvidenceAudit" in js
    assert "async function runHotEvidenceAudit" in js
    assert 'setInterval(runHotEvidenceAudit' not in js

def test_backup_restrictions_preserved():
    html=(ROOT/'control_center/templates/index.html').read_text(encoding='utf-8')
    assert 'hot-evidence-audit-run' in html
    assert 'storage-preview-delete' in html
    app=(ROOT/'control_center/app.py').read_text(encoding='utf-8')
    assert '@app.get("/api/storage/hot-evidence-audit")' in app
