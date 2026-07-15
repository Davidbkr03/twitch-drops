from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_bcrypt import Bcrypt
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_socketio import SocketIO
from flask_wtf.csrf import CSRFProtect

db = SQLAlchemy()
login_manager = LoginManager()
bcrypt = Bcrypt()
socketio = SocketIO()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, default_limits=[])
