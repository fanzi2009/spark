#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from pyspark.sql.connect.utils import check_dependencies

check_dependencies(__name__, __file__)

import os
import warnings
from collections.abc import Sized
from distutils.version import LooseVersion
from functools import reduce
from threading import RLock
from typing import (
    Optional,
    Any,
    Union,
    Dict,
    List,
    Tuple,
    cast,
    overload,
    Iterable,
    TYPE_CHECKING,
)

import numpy as np
import pandas as pd
import pyarrow as pa
from pandas.api.types import (  # type: ignore[attr-defined]
    is_datetime64_dtype,
    is_datetime64tz_dtype,
)

from pyspark import SparkContext, SparkConf, __version__
from pyspark.sql.connect.client import SparkConnectClient
from pyspark.sql.connect.dataframe import DataFrame
from pyspark.sql.connect.plan import SQL, Range, LocalRelation
from pyspark.sql.connect.readwriter import DataFrameReader
from pyspark.sql.pandas.serializers import ArrowStreamPandasSerializer
from pyspark.sql.pandas.types import to_arrow_type, _get_local_timezone
from pyspark.sql.session import classproperty, SparkSession as PySparkSession
from pyspark.sql.types import (
    _infer_schema,
    _has_nulltype,
    _merge_type,
    Row,
    DataType,
    StructType,
    AtomicType,
    TimestampType,
)
from pyspark.sql.utils import to_str

if TYPE_CHECKING:
    from pyspark.sql.connect._typing import OptionalPrimitiveType
    from pyspark.sql.connect.catalog import Catalog
    from pyspark.sql.connect.udf import UDFRegistration


class SparkSession:
    class Builder:
        """Builder for :class:`SparkSession`."""

        _lock = RLock()

        def __init__(self) -> None:
            self._options: Dict[str, Any] = {}

        @overload
        def config(self, key: str, value: Any) -> "SparkSession.Builder":
            ...

        @overload
        def config(self, *, map: Dict[str, "OptionalPrimitiveType"]) -> "SparkSession.Builder":
            ...

        def config(
            self,
            key: Optional[str] = None,
            value: Optional[Any] = None,
            *,
            map: Optional[Dict[str, "OptionalPrimitiveType"]] = None,
        ) -> "SparkSession.Builder":
            with self._lock:
                if map is not None:
                    for k, v in map.items():
                        self._options[k] = to_str(v)
                else:
                    self._options[cast(str, key)] = to_str(value)
                return self

        def master(self, master: str) -> "SparkSession.Builder":
            return self

        def appName(self, name: str) -> "SparkSession.Builder":
            return self.config("spark.app.name", name)

        def remote(self, location: str = "sc://localhost") -> "SparkSession.Builder":
            return self.config("spark.remote", location)

        def enableHiveSupport(self) -> "SparkSession.Builder":
            raise NotImplementedError("enableHiveSupport not implemented for Spark Connect")

        def getOrCreate(self) -> "SparkSession":
            return SparkSession(connectionString=self._options["spark.remote"])

    _client: SparkConnectClient

    @classproperty
    def builder(cls) -> Builder:
        """Creates a :class:`Builder` for constructing a :class:`SparkSession`."""
        return cls.Builder()

    def __init__(self, connectionString: str, userId: Optional[str] = None):
        """
        Creates a new SparkSession for the Spark Connect interface.

        Parameters
        ----------
        connectionString: str, optional
            Connection string that is used to extract the connection parameters and configure
            the GRPC connection. Defaults to `sc://localhost`.
        userId : str, optional
            Optional unique user ID that is used to differentiate multiple users and
            isolate their Spark Sessions. If the `user_id` is not set, will default to
            the $USER environment. Defining the user ID as part of the connection string
            takes precedence.
        """
        # Parse the connection string.
        self._client = SparkConnectClient(connectionString)

    def table(self, tableName: str) -> DataFrame:
        return self.read.table(tableName)

    table.__doc__ = PySparkSession.table.__doc__

    @property
    def read(self) -> "DataFrameReader":
        return DataFrameReader(self)

    read.__doc__ = PySparkSession.read.__doc__

    def _inferSchemaFromList(
        self, data: Iterable[Any], names: Optional[List[str]] = None
    ) -> StructType:
        """
        Infer schema from list of Row, dict, or tuple.

        Refer to 'pyspark.sql.session._inferSchemaFromList' with default configurations:

          - 'infer_dict_as_struct' : False
          - 'infer_array_from_first_element' : False
          - 'prefer_timestamp_ntz' : False
        """
        if not data:
            raise ValueError("can not infer schema from empty dataset")
        infer_dict_as_struct = False
        infer_array_from_first_element = False
        prefer_timestamp_ntz = False
        return reduce(
            _merge_type,
            (
                _infer_schema(
                    row,
                    names,
                    infer_dict_as_struct=infer_dict_as_struct,
                    infer_array_from_first_element=infer_array_from_first_element,
                    prefer_timestamp_ntz=prefer_timestamp_ntz,
                )
                for row in data
            ),
        )

    def createDataFrame(
        self,
        data: Union["pd.DataFrame", "np.ndarray", Iterable[Any]],
        schema: Optional[Union[AtomicType, StructType, str, List[str], Tuple[str, ...]]] = None,
    ) -> "DataFrame":
        assert data is not None
        if isinstance(data, DataFrame):
            raise TypeError("data is already a DataFrame")

        _schema: Optional[Union[AtomicType, StructType]] = None
        _schema_str: Optional[str] = None
        _cols: Optional[List[str]] = None
        _num_cols: Optional[int] = None

        if isinstance(schema, (AtomicType, StructType)):
            _schema = schema
            if isinstance(schema, StructType):
                _num_cols = len(schema.fields)
            else:
                _num_cols = 1

        elif isinstance(schema, str):
            _schema_str = schema

        elif isinstance(schema, (list, tuple)):
            # Must re-encode any unicode strings to be consistent with StructField names
            _cols = [x.encode("utf-8") if not isinstance(x, str) else x for x in schema]
            _num_cols = len(_cols)

        if isinstance(data, Sized) and len(data) == 0:
            if _schema is not None:
                return DataFrame.withPlan(LocalRelation(table=None, schema=_schema.json()), self)
            elif _schema_str is not None:
                return DataFrame.withPlan(LocalRelation(table=None, schema=_schema_str), self)
            else:
                raise ValueError("can not infer schema from empty dataset")

        _table: Optional[pa.Table] = None
        _inferred_schema: Optional[StructType] = None

        if isinstance(data, pd.DataFrame):
            # Logic was borrowed from `_create_from_pandas_with_arrow` in
            # `pyspark.sql.pandas.conversion.py`. Should ideally deduplicate the logics.

            # If no schema supplied by user then get the names of columns only
            if schema is None:
                _cols = [str(x) if not isinstance(x, str) else x for x in data.columns]

            # Determine arrow types to coerce data when creating batches
            if isinstance(schema, StructType):
                arrow_types = [to_arrow_type(f.dataType) for f in schema.fields]
                _cols = [str(x) if not isinstance(x, str) else x for x in schema.fieldNames()]
            elif isinstance(schema, DataType):
                raise ValueError("Single data type %s is not supported with Arrow" % str(schema))
            else:
                # Any timestamps must be coerced to be compatible with Spark
                arrow_types = [
                    to_arrow_type(TimestampType())
                    if is_datetime64_dtype(t) or is_datetime64tz_dtype(t)
                    else None
                    for t in data.dtypes
                ]

            ser = ArrowStreamPandasSerializer(
                _get_local_timezone(),  # 'spark.session.timezone' should be respected
                False,  # 'spark.sql.execution.pandas.convertToArrowArraySafely' should be respected
                True,
            )

            _table = pa.Table.from_batches(
                [ser._create_batch([(c, t) for (_, c), t in zip(data.items(), arrow_types)])]
            )

        elif isinstance(data, np.ndarray):
            if data.ndim not in [1, 2]:
                raise ValueError("NumPy array input should be of 1 or 2 dimensions.")

            if _cols is None:
                if data.ndim == 1 or data.shape[1] == 1:
                    _cols = ["value"]
                else:
                    _cols = ["_%s" % i for i in range(1, data.shape[1] + 1)]

            if data.ndim == 1:
                if 1 != len(_cols):
                    raise ValueError(
                        f"Length mismatch: Expected axis has 1 element, "
                        f"new values have {len(_cols)} elements"
                    )

                _table = pa.Table.from_arrays([pa.array(data)], _cols)
            else:
                if data.shape[1] != len(_cols):
                    raise ValueError(
                        f"Length mismatch: Expected axis has {data.shape[1]} elements, "
                        f"new values have {len(_cols)} elements"
                    )

                _table = pa.Table.from_arrays(
                    [pa.array(data[::, i]) for i in range(0, data.shape[1])], _cols
                )

        else:
            _data = list(data)

            if isinstance(_data[0], dict):
                # Sort the data to respect inferred schema.
                # For dictionaries, we sort the schema in alphabetical order.
                _data = [dict(sorted(d.items())) for d in _data]

            elif not isinstance(_data[0], (Row, tuple, list, dict)) and not hasattr(
                _data[0], "__dict__"
            ):
                # input data can be [1, 2, 3]
                # we need to convert it to [[1], [2], [3]] to be able to infer schema.
                _data = [[d] for d in _data]

            _inferred_schema = self._inferSchemaFromList(_data, _cols)

            if _has_nulltype(_inferred_schema):
                # For cases like createDataFrame([("Alice", None, 80.1)], schema)
                # we can not infer the schema from the data itself.
                warnings.warn("failed to infer the schema from data")
                if _schema is None or not isinstance(_schema, StructType):
                    raise ValueError(
                        "Some of types cannot be determined after inferring, "
                        "a StructType Schema is required in this case"
                    )
                _inferred_schema = _schema

            from pyspark.sql.connect.conversion import LocalDataToArrowConversion

            # Spark Connect will try its best to build the Arrow table with the
            # inferred schema in the client side, and then rename the columns and
            # cast the datatypes in the server side.
            _table = LocalDataToArrowConversion.convert(_data, _inferred_schema)

        # TODO: Beside the validation on number of columns, we should also check
        # whether the Arrow Schema is compatible with the user provided Schema.
        if _num_cols is not None and _num_cols != _table.shape[1]:
            raise ValueError(
                f"Length mismatch: Expected axis has {_num_cols} elements, "
                f"new values have {_table.shape[1]} elements"
            )

        if _schema is not None:
            return DataFrame.withPlan(LocalRelation(_table, schema=_schema.json()), self)
        elif _schema_str is not None:
            return DataFrame.withPlan(LocalRelation(_table, schema=_schema_str), self)
        elif _cols is not None and len(_cols) > 0:
            return DataFrame.withPlan(LocalRelation(_table), self).toDF(*_cols)
        else:
            return DataFrame.withPlan(LocalRelation(_table), self)

    createDataFrame.__doc__ = PySparkSession.createDataFrame.__doc__

    def sql(self, sqlQuery: str, args: Optional[Dict[str, str]] = None) -> "DataFrame":
        return DataFrame.withPlan(SQL(sqlQuery, args), self)

    sql.__doc__ = PySparkSession.sql.__doc__

    def range(
        self,
        start: int,
        end: Optional[int] = None,
        step: int = 1,
        numPartitions: Optional[int] = None,
    ) -> DataFrame:
        if end is None:
            actual_end = start
            start = 0
        else:
            actual_end = end

        if numPartitions is not None:
            numPartitions = int(numPartitions)

        return DataFrame.withPlan(
            Range(
                start=int(start), end=int(actual_end), step=int(step), num_partitions=numPartitions
            ),
            self,
        )

    range.__doc__ = PySparkSession.range.__doc__

    @property
    def catalog(self) -> "Catalog":
        from pyspark.sql.connect.catalog import Catalog

        if not hasattr(self, "_catalog"):
            self._catalog = Catalog(self)
        return self._catalog

    catalog.__doc__ = PySparkSession.catalog.__doc__

    def __del__(self) -> None:
        try:
            # Try its best to close.
            self.client.close()
        except Exception:
            pass

    def stop(self) -> None:
        # Stopping the session will only close the connection to the current session (and
        # the life cycle of the session is maintained by the server),
        # whereas the regular PySpark session immediately terminates the Spark Context
        # itself, meaning that stopping all Spark sessions.
        # It is controversial to follow the existing the regular Spark session's behavior
        # specifically in Spark Connect the Spark Connect server is designed for
        # multi-tenancy - the remote client side cannot just stop the server and stop
        # other remote clients being used from other users.
        self.client.close()

        if "SPARK_LOCAL_REMOTE" in os.environ:
            # When local mode is in use, follow the regular Spark session's
            # behavior by terminating the Spark Connect server,
            # meaning that you can stop local mode, and restart the Spark Connect
            # client with a different remote address.
            active_session = PySparkSession.getActiveSession()
            if active_session is not None:
                active_session.stop()
            with SparkContext._lock:
                del os.environ["SPARK_LOCAL_REMOTE"]
                del os.environ["SPARK_REMOTE"]

    stop.__doc__ = PySparkSession.stop.__doc__

    @classmethod
    def getActiveSession(cls) -> Any:
        raise NotImplementedError("getActiveSession() is not implemented.")

    def newSession(self) -> Any:
        raise NotImplementedError("newSession() is not implemented.")

    @property
    def conf(self) -> Any:
        raise NotImplementedError("conf() is not implemented.")

    @property
    def sparkContext(self) -> Any:
        raise NotImplementedError("sparkContext() is not implemented.")

    @property
    def streams(self) -> Any:
        raise NotImplementedError("streams() is not implemented.")

    @property
    def readStream(self) -> Any:
        raise NotImplementedError("readStream() is not implemented.")

    @property
    def udf(self) -> "UDFRegistration":
        from pyspark.sql.connect.udf import UDFRegistration

        return UDFRegistration(self)

    udf.__doc__ = PySparkSession.udf.__doc__

    @property
    def version(self) -> str:
        raise NotImplementedError("version() is not implemented.")

    # SparkConnect-specific API
    @property
    def client(self) -> "SparkConnectClient":
        """
        Gives access to the Spark Connect client. In normal cases this is not necessary to be used
        and only relevant for testing.
        Returns
        -------
        :class:`SparkConnectClient`
        """
        return self._client

    @staticmethod
    def _start_connect_server(master: str, opts: Dict[str, Any]) -> None:
        """
        Starts the Spark Connect server given the master (thread-unsafe).

        At the high level, there are two cases. The first case is development case, e.g.,
        you locally build Apache Spark, and run ``SparkSession.builder.remote("local")``:

        1. This method automatically finds the jars for Spark Connect (because the jars for
          Spark Connect are not bundled in the regular Apache Spark release).

        2. Temporarily remove all states for Spark Connect, for example, ``SPARK_REMOTE``
          environment variable.

        3. Starts a JVM (without Spark Context) first, and adds the Spark Connect server jars
           into the current class loader. Otherwise, Spark Context with ``spark.plugins``
           cannot be initialized because the JVM is already running without the jars in
           the classpath before executing this Python process for driver side (in case of
           PySpark application submission).

        4. Starts a regular Spark session that automatically starts a Spark Connect server
           via ``spark.plugins`` feature.

        The second case is when you use Apache Spark release:

        1. Users must specify either the jars or package, e.g., ``--packages
          org.apache.spark:spark-connect_2.12:3.4.0``. The jars or packages would be specified
          in SparkSubmit automatically. This method does not do anything related to this.

        2. Temporarily remove all states for Spark Connect, for example, ``SPARK_REMOTE``
          environment variable. It does not do anything for PySpark application submission as
          well because jars or packages were already specified before executing this Python
          process for driver side.

        3. Starts a regular Spark session that automatically starts a Spark Connect server
          with JVM via ``spark.plugins`` feature.
        """
        session = PySparkSession._instantiatedSession
        if session is None or session._sc._jsc is None:

            # Configurations to be overwritten
            overwrite_conf = opts
            overwrite_conf["spark.master"] = master
            overwrite_conf["spark.local.connect"] = "1"

            # Configurations to be set if unset.
            default_conf = {"spark.plugins": "org.apache.spark.sql.connect.SparkConnectPlugin"}

            if "SPARK_TESTING" in os.environ:
                # For testing, we use 0 to use an ephemeral port to allow parallel testing.
                # See also SPARK-42272.
                overwrite_conf["spark.connect.grpc.binding.port"] = "0"

            def create_conf(**kwargs: Any) -> SparkConf:
                conf = SparkConf(**kwargs)
                for k, v in overwrite_conf.items():
                    conf.set(k, v)
                for k, v in default_conf.items():
                    if not conf.contains(k):
                        conf.set(k, v)
                return conf

            # Check if we're using unreleased version that is in development.
            # Also checks SPARK_TESTING for RC versions.
            is_dev_mode = (
                "dev" in LooseVersion(__version__).version or "SPARK_TESTING" in os.environ
            )

            origin_remote = os.environ.get("SPARK_REMOTE", None)
            try:
                if origin_remote is not None:
                    # So SparkSubmit thinks no remote is set in order to
                    # start the regular PySpark session.
                    del os.environ["SPARK_REMOTE"]

                SparkContext._ensure_initialized(conf=create_conf(loadDefaults=False))

                if is_dev_mode:
                    # Try and catch for a possibility in production because pyspark.testing
                    # does not exist in the canonical release.
                    try:
                        from pyspark.testing.utils import search_jar

                        # Note that, in production, spark.jars.packages configuration should be
                        # set by users. Here we're automatically searching the jars locally built.
                        connect_jar = search_jar(
                            "connector/connect/server", "spark-connect-assembly-", "spark-connect"
                        )
                        if connect_jar is None:
                            warnings.warn(
                                "Attempted to automatically find the Spark Connect jars because "
                                "'SPARK_TESTING' environment variable is set, or the current "
                                f"PySpark version is dev version ({__version__}). However, the jar"
                                " was not found. Manually locate the jars and specify them, e.g., "
                                "'spark.jars' configuration."
                            )
                        else:
                            pyutils = SparkContext._jvm.PythonSQLUtils  # type: ignore[union-attr]
                            pyutils.addJarToCurrentClassLoader(connect_jar)

                    except ImportError:
                        pass

                # The regular PySpark session is registered as an active session
                # so would not be garbage-collected.
                PySparkSession(
                    SparkContext.getOrCreate(create_conf(loadDefaults=True, _jvm=SparkContext._jvm))
                )
            finally:
                if origin_remote is not None:
                    os.environ["SPARK_REMOTE"] = origin_remote
        else:
            raise RuntimeError("There should not be an existing Spark Session or Spark Context.")


SparkSession.__doc__ = PySparkSession.__doc__


def _test() -> None:
    import sys
    import doctest
    from pyspark.sql import SparkSession as PySparkSession
    import pyspark.sql.connect.session

    globs = pyspark.sql.connect.session.__dict__.copy()
    globs["spark"] = (
        PySparkSession.builder.appName("sql.connect.session tests").remote("local[4]").getOrCreate()
    )

    # Uses PySpark session to test builder.
    globs["SparkSession"] = PySparkSession
    # Spark Connect does not support to set master together.
    pyspark.sql.connect.session.SparkSession.__doc__ = None
    del pyspark.sql.connect.session.SparkSession.Builder.master.__doc__
    # RDD API is not supported in Spark Connect.
    del pyspark.sql.connect.session.SparkSession.createDataFrame.__doc__

    # TODO(SPARK-41811): Implement SparkSession.sql's string formatter
    del pyspark.sql.connect.session.SparkSession.sql.__doc__

    (failure_count, test_count) = doctest.testmod(
        pyspark.sql.connect.session,
        globs=globs,
        optionflags=doctest.ELLIPSIS
        | doctest.NORMALIZE_WHITESPACE
        | doctest.IGNORE_EXCEPTION_DETAIL,
    )

    globs["spark"].stop()

    if failure_count:
        sys.exit(-1)


if __name__ == "__main__":
    _test()