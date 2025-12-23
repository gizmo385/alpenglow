FROM postgres:17.5

RUN apt update && apt install -y --no-install-recommends wget ca-certificates postgresql-common
RUN /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -i -v 17
RUN apt update && apt install -y --no-install-recommends postgresql-17-pgvector
RUN wget -O /tmp/vchord.deb https://github.com/tensorchord/VectorChord/releases/download/0.3.0/postgresql-17-vchord_0.3.0-1_$(dpkg --print-architecture).deb && \
	apt install /tmp/vchord.deb && \
	rm -rf /tmp/vchord.deb
