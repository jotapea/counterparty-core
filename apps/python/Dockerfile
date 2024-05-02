
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y python3 python3-dev python3-pip

# copy app files, install and run

RUN mkdir /app
COPY . /app
WORKDIR /app

RUN pip3 install -r requirements.txt

EXPOSE 3000
CMD [ "flask", "--app", "server", "run", "--host", "0.0.0.0", "--port", "3000" ]
