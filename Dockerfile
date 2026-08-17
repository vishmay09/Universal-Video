FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /home/user/app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p video_processing_workspace1 && chmod -R 777 video_processing_workspace1

EXPOSE 7860

ENV PORT=7860

CMD ["python", "final1.py"]
