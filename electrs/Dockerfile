
FROM ubuntu:22.04

RUN apt-get update && apt-get install -y build-essential pkg-config libssl-dev curl librocksdb-dev cargo clang cmake

RUN adduser --disabled-login --system --shell /bin/false --uid 1000 user
WORKDIR /home/user

COPY . .

RUN cargo build --bin electrs --locked --release

ENV RUST_BACKTRACE=1
ENV RUST_LOG=info

EXPOSE 50001

# https://github.com/romanz/electrs/blob/master/doc/config.md#configuration-files-and-priorities

CMD ["target/release/electrs", "--conf", "config.toml"]
