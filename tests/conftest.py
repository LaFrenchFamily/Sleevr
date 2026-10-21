import os
import sys
import tempfile

# base de test isolée, pas de relevé automatique des prix
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="holocarte-test-"))
os.environ["DISABLE_SCHEDULER"] = "1"
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
