from flask import Flask, request, send_file
from PIL import Image
import io

app = Flask(__name__)

@app.route('/watermark', methods=['POST'])
def watermark():

    foto = Image.open(request.files['foto']).convert("RGBA")
    logo = Image.open("logo.png").convert("RGBA")

    logo.thumbnail((300,300))

    foto.paste(
        logo,
        (
            foto.width - logo.width - 30,
            foto.height - logo.height - 30
        ),
        logo
    )

    buffer = io.BytesIO()

    foto.convert("RGB").save(
        buffer,
        format="WEBP",
        quality=85
    )

    buffer.seek(0)

    return send_file(
        buffer,
        mimetype="image/webp"
    )

app.run(host="0.0.0.0", port=5000)
