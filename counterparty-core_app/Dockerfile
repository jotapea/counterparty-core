
FROM ubuntu:22.04

RUN apt-get update
RUN apt-get install -y python3 python3-dev python3-pip

# copy flask app
COPY ./flask_app /app

# install
WORKDIR /app
RUN pip3 install -r requirements.txt
# ERROR: Directory '.' is not installable. Neither 'setup.py' nor 'pyproject.toml' found.
# RUN pip3 install .

# exposing server port
EXPOSE 3001

# run
CMD [ "flask", "--app", "server", "run", "--host", "0.0.0.0", "--port", "3001" ]
# CMD [ "flask", "--app", "server", "run", "--port", "3001" ]
