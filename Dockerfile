FROM python:3.12-slim
WORKDIR /app

# setuptools needs the package directory to exist when it builds the project.
# The previous image tried `pip install .` before copying app/, which caused
# every Railway service built from this Dockerfile to fail.
COPY pyproject.toml .
COPY app ./app
RUN pip install --no-cache-dir .

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
