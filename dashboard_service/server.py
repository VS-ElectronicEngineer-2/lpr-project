from waitress import serve
from dashboard import app  # Change to match your main Flask file name

if __name__ == "__main__":
    serve(app, host="0.0.0.0", port=5002)
