import sys
import os

# Add project directory to the sys.path
project_home = os.path.dirname(os.path.abspath(__file__))
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Import the Flask app object and expose it as app and application
from app import app
application = app
