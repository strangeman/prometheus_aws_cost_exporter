FROM python:3.6-alpine

WORKDIR /usr/src/app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
ENV FLASK_APP app.py
ENV TZ Asia/Kolkata
ENTRYPOINT [ "flask", "run", "--host=0.0.0.0" ]
CMD []
