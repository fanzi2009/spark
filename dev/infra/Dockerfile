FROM ubuntu:22.04

ENV FULL_REFRESH_DATE 20221118

ENV DEBIAN_FRONTEND noninteractive
ENV DEBCONF_NONINTERACTIVE_SEEN true

ARG APT_INSTALL="apt-get install --no-install-recommends -y"

RUN apt-get clean
RUN apt-get update
RUN $APT_INSTALL software-properties-common git libxml2-dev pkg-config curl wget openjdk-11-jdk libpython3-dev python3-pip python3-setuptools python3.10 python3-pip gnupg2 libfreetype6-dev

RUN $APT_INSTALL cmake pkg-config libboost-dev libboost-system-dev libboost-chrono-dev libboost-program-options-dev libboost-thread-dev libboost-iostreams-dev libboost-regex-dev libboost-fiber-dev libtbb-dev libspdlog-dev libgtest-dev libbenchmark-dev libmsgsl-dev openssl libssl-dev libnuma-dev libjemalloc-dev python3.10-venv ssh curl zip

RUN add-apt-repository ppa:pypy/ppa
RUN apt update
RUN $APT_INSTALL gfortran libopenblas-dev liblapack-dev
RUN $APT_INSTALL build-essential
RUN rm -rf /usr/local/lib/python3.10/dist-packages/*
RUN $APT_INSTALL python3-matplotlib

RUN $APT_INSTALL gnupg ca-certificates pandoc

RUN python3 -m pip install --ignore-installed 'numpy==1.23.5' 'pyarrow==12.0.1' 'pandas<=1.5.3' scipy unittest-xml-reporting plotly>=4.8 scikit-learn 'mlflow>=1.0' coverage 'matplotlib<3.3.0' openpyxl 'memory-profiler==0.60.0' 'scikit-learn==1.1.*'

# Add Python deps for Spark Connect.
RUN python3 -m pip install grpcio protobuf googleapis-common-protos grpcio-status

# Add torch as a testing dependency for TorchDistributor
RUN python3 -m pip install torch torchvision

RUN curl -fL https://github.com/coursier/coursier/releases/latest/download/cs-x86_64-pc-linux.gz | gzip -d > cs && chmod +x cs && ./cs setup --yes

RUN curl -s https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86_64.sh > miniconda.sh
RUN bash miniconda.sh -b -p $HOME/miniconda
