import sys
import os
from dotenv import load_dotenv

# Define the project root directory
project_home = os.path.dirname(os.path.abspath(__file__))
if project_home not in sys.path:
    sys.path.insert(0, project_home)

# Load environment variables from the absolute path
dotenv_path = os.path.join(project_home, '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)

from app import app
application = app
