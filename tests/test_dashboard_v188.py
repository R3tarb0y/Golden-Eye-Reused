from pathlib import Path
import ast

ROOT=Path(__file__).resolve().parents[1]
APP=(ROOT/'dashboard/app.py').read_text(encoding='utf-8')
DB=(ROOT/'dashboard/db.py').read_text(encoding='utf-8')

def _function(name):
    module=ast.parse(APP)
    return next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name==name)

def test_valid_python():
    ast.parse(APP); ast.parse(DB)

def test_callback_output_count_stable():
    for name, count in [('render_all',28),('render_harmony_transaction_validation',18)]:
        node=_function(name)
        decorators=ast.get_source_segment(APP,node.decorator_list[0])
        assert decorators.count('Output(')==count, (name,count)

def test_transaction_gate_precedes_database_fetch():
    node=_function('render_harmony_transaction_validation')
    text=ast.get_source_segment(APP,node)
    assert text.find('!= "harmony-transaction"') < text.find('load_harmony_transaction_validation(')
    assert 'return (no_update,) * 18' in text

def test_reused_gate_precedes_database_fetch():
    text=ast.get_source_segment(APP,_function('render_all'))
    assert text.find('!= "reused-dashboard"') < text.find('load_groups(')
    assert 'return (no_update,) * 28' in text

def test_no_transaction_duplicate_store():
    body=ast.get_source_segment(APP,_function('render_harmony_transaction_validation'))
    assert 'TRANSACTION_TABLE_COLUMNS,' in body and '# 5,000 full transaction records' in body and '[],\n    )' in body
    assert '"HARMONY_TRANSACTION_TABLE_LIMIT", "500"' in APP

def test_timing_and_safe_sql_logging():
    assert 'execute_ms' in DB and 'fetch_ms' in DB and 'frame_ms' in DB
    assert 'cur.mogrify(' not in DB
    assert 'relations={relations}' in DB
