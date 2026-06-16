from flask import Flask

app = Flask(__name__)

@app.route('/')
def home():
    return "Python Flask serverless function deployment is active and working successfully!"
