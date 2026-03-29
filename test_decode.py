import base64
test='731da81434724212b2fa7761adbfd36b'
try:
    decoded = base64.b64decode(test).decode()
    print('Decoded:', decoded)
except Exception as e:
    print('Not valid base64 or not decodable as UTF-8:', e)
