import mysql.connector
from mysql.connector import pooling
import configparser
from threading import Thread
import os
import traceback  # Para obtener detalles más específicos del error

# Configuración de la base de datos
config_path = os.path.join(os.path.dirname(__file__), 'config.ini')
if not os.path.exists(config_path):
    raise FileNotFoundError(f"El archivo config.ini no se encontró en {config_path}")

config = configparser.ConfigParser()
config.read(config_path)

db_config_remote = {
    'host': config['mysql']['host_remote'],
    'port': int(config['mysql']['port']),
    'user': config['mysql']['user'],
    'password': config['mysql']['password'],
    'database': config['mysql']['database_remote'],
}

db_config_local = {
    'host': config['mysql']['host_local'],
    'port': int(config['mysql']['port']),
    'user': config['mysql']['user'],
    'password': config['mysql']['password'],
    'database': config['mysql']['database_local'],
}

#Conectarse a MySQL sin base de datos para crear la local si no existe
try:
    conn = mysql.connector.connect(
        host=config['mysql']['host_local'],
        port=int(config['mysql']['port']),
        user=config['mysql']['user'],
        password=config['mysql']['password']
    )
    cursor = conn.cursor()
    cursor.execute(f"CREATE DATABASE IF NOT EXISTS {config['mysql']['database_local']};")
    cursor.close()
    conn.close()
    print(f"Base de datos {config['mysql']['database_local']} verificada o creada.")
except Exception as e:
    print(f"Error al verificar/crear la base local: {e}")

# Crear los pools de conexiones
try:
    remote_pool = pooling.MySQLConnectionPool(
        pool_name="remote_pool",
        pool_size=5,
        **db_config_remote
    )
    print("Conexión remota establecida.")
except Exception as e:
    remote_pool = None
    print(f"No se pudo establecer conexión con la base remota: {e}")

try:
    local_pool = pooling.MySQLConnectionPool(
        pool_name="local_pool",
        pool_size=5,
        **db_config_local
    )
    print("Conexión local establecida.")
except Exception as e:
    raise Exception(f"No se pudo establecer conexión con la base local: {e}")

def get_connection(pool):
    """Obtiene una conexión del pool especificado."""
    return pool.get_connection()

def save_record(table_name, data, callback=None):
    """Guarda un registro en ambas bases de datos (local y remota) y almacena la relación de IDs."""
    def db_task():
        local_id = None
        remote_id = None

        # Guardar en la base local
        conn_local = None
        cursor_local = None
        try:
            conn_local = get_connection(local_pool)
            cursor_local = conn_local.cursor()
            columns = ', '.join(f"`{col}`" for col in data.keys())
            placeholders = ', '.join(['%s'] * len(data))
            values = tuple(data.values())
            query = f"INSERT INTO {table_name} ({columns}) VALUES ({placeholders});"
            cursor_local.execute(query, values)
            conn_local.commit()
            local_id = cursor_local.lastrowid  # Obtener el ID insertado en la base local
            print(f"✅ Registro guardado en la base local con ID={local_id}")
        except Exception as e:
            print(f"❌ Error al guardar en la base local: {e}")
        finally:
            if cursor_local:
                cursor_local.close()
            if conn_local:
                conn_local.close()

        # Guardar en la base remota
        conn_remote = None
        cursor_remote = None
        if remote_pool:
            try:
                conn_remote = get_connection(remote_pool)
                cursor_remote = conn_remote.cursor()
                cursor_remote.execute(query, values)  # Reutilizamos la consulta
                conn_remote.commit()
                remote_id = cursor_remote.lastrowid  # Obtener el ID insertado en la base remota
                print(f"✅ Registro guardado en la base remota con ID={remote_id}")
            except Exception as e:
                print(f"⚠ Advertencia: No se pudo guardar en la base remota: {e}")
            finally:
                if cursor_remote:
                    cursor_remote.close()
                if conn_remote:
                    conn_remote.close()

        # Guardar la relación de IDs en la base local
        if local_id is not None and remote_id is not None:
            try:
                conn_local = get_connection(local_pool)
                cursor_local = conn_local.cursor()
                cursor_local.execute(
                    "INSERT INTO id_mapping (local_id, remote_id, tabla) VALUES (%s, %s, %s)",
                    (local_id, remote_id, table_name)
                )
                conn_local.commit()
                print(f"🔗 Mapeo de ID registrado: local_id={local_id}, remote_id={remote_id}, tabla={table_name}")
            except Exception as e:
                print(f"❌ Error al guardar el mapeo de ID: {e}")
            finally:
                if cursor_local:
                    cursor_local.close()
                if conn_local:
                    conn_local.close()

        if callback:
            callback(True, None, local_id)

    Thread(target=db_task).start()

def fetch_last_records(table_name, limit, callback):
    """Obtiene los últimos registros desde la base de datos local."""
    def db_task():
        conn = None
        cursor = None
        try:
            conn = get_connection(local_pool)
            cursor = conn.cursor()
            query = f"SELECT * FROM {table_name} ORDER BY id DESC LIMIT {limit};"
            cursor.execute(query)
            results = cursor.fetchall()
            callback(results, None)
        except Exception as e:
            print(f"Error al obtener los registros de la base local: {e}")
            callback(None, e)
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    Thread(target=db_task).start()

def sync_local_to_remote():
    """Sincroniza los datos de todas las tablas de la base local a la remota."""
    if not remote_pool:
        print("❌ Sincronización omitida: Conexión remota no disponible.")
        return

    def db_task():
        conn_local = None
        cursor_local = None
        conn_remote = None
        cursor_remote = None
        try:
            conn_local = get_connection(local_pool)
            cursor_local = conn_local.cursor()

            # 💡 Verificar si está accediendo a la base correcta
            cursor_local.execute("SELECT DATABASE();")
            database_name = cursor_local.fetchone()
            print(f"🔍 Conectado a la base local: {database_name}")

            # Obtener lista de tablas de la base local
            cursor_local.execute("SHOW TABLES;")
            tables = cursor_local.fetchall()

            if not tables:
                print("⚠️ No se encontraron tablas en la base local. Verifica si se crearon correctamente.")
                return

            tables = [row[0] for row in tables]  # Convertir resultado a lista de nombres de tablas
            print(f"🔍 Tablas detectadas en la base local: {tables}")

            conn_remote = get_connection(remote_pool)
            cursor_remote = conn_remote.cursor()

            for table_name in tables:
                print(f"📤 Sincronizando tabla: {table_name}")

                # Obtener todos los registros locales
                cursor_local.execute(f"SELECT * FROM {table_name};")
                local_records = cursor_local.fetchall()
                
                if not local_records:
                    print(f"⚠️ No hay registros en la tabla {table_name}, saltando...")
                    continue

                print(f"📌 {len(local_records)} registros encontrados en {table_name}")

                # Obtener columnas de la tabla
                cursor_local.execute(f"SHOW COLUMNS FROM {table_name};")
                columns = [column[0] for column in cursor_local.fetchall()]
                print(f"🛠️ Columnas de {table_name}: {columns}")

                # Construir consulta para insertar en la base remota
                placeholders = ', '.join(['%s'] * len(columns))
                column_names = ', '.join(f"`{col}`" for col in columns)
                insert_query = f"INSERT IGNORE INTO {table_name} ({column_names}) VALUES ({placeholders})"
                print(f"💾 Query generada: {insert_query}")

                # Insertar registros en la base remota
                for record in local_records:
                    values = tuple(record)
                    try:
                        cursor_remote.execute(insert_query, values)
                    except Exception as insert_error:
                        print(f"❌ Error insertando en {table_name}: {insert_error}")

            conn_remote.commit()
            print("✅ Sincronización completada con éxito.")
        except Exception as e:
            print(f"❌ Error durante la sincronización: {e}")
        finally:
            if cursor_local:
                cursor_local.close()
            if conn_local:
                conn_local.close()
            if cursor_remote:
                cursor_remote.close()
            if conn_remote:
                conn_remote.close()

    Thread(target=db_task).start()

def get_last_value(table_name, column_name, callback):
    """Obtiene el último valor de una columna específica en una tabla."""
    def db_task():
        conn = None
        cursor = None
        try:
            conn = get_connection(local_pool)
            cursor = conn.cursor()
            query = f"SELECT {column_name} FROM {table_name} ORDER BY id DESC LIMIT 1;"
            cursor.execute(query)
            result = cursor.fetchone()
            value = result[0] if result and result[0] is not None else None
            callback(value, None)
        except Exception as e:
            print(f"Error al obtener el valor de {column_name}: {e}")
            callback(None, e)
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

    Thread(target=db_task).start()

def update_record(table_name, record_id, data, callback=None):
    """Actualiza un registro en ambas bases de datos usando la relación de ID local-remoto."""
    def db_task():
        remote_id = None

        # Buscar el remote_id en la base local
        conn_local = None
        cursor_local = None
        try:
            conn_local = get_connection(local_pool)
            cursor_local = conn_local.cursor()
            cursor_local.execute(
                "SELECT remote_id FROM id_mapping WHERE local_id = %s AND tabla = %s",
                (record_id, table_name)
            )
            result = cursor_local.fetchone()
            if result:
                remote_id = result[0]  # Obtener el ID de la base remota
        except Exception as e:
            print(f"⚠ Error al obtener remote_id para {table_name}: {e}")
        finally:
            if cursor_local:
                cursor_local.close()
            if conn_local:
                conn_local.close()

        # Actualizar en la base local
        conn = None
        cursor = None
        try:
            conn = get_connection(local_pool)
            cursor = conn.cursor()
            set_clause = ', '.join(f"`{col}` = %s" for col in data.keys())
            values = tuple(data.values())
            query = f"UPDATE {table_name} SET {set_clause} WHERE ID = %s;"
            cursor.execute(query, values + (record_id,))
            conn.commit()
            print(f"✅ Registro actualizado en la base local (ID={record_id})")
        except Exception as e:
            print(f"❌ Error al actualizar en la base local: {e}")
        finally:
            if cursor:
                cursor.close()
            if conn:
                conn.close()

        # Actualizar en la base remota si tenemos el ID correcto
        if remote_pool and remote_id is not None:
            conn = None
            cursor = None
            try:
                conn = get_connection(remote_pool)
                cursor = conn.cursor()
                cursor.execute(query, values + (remote_id,))
                conn.commit()
                print(f"✅ Registro actualizado en la base remota (ID={remote_id})")
            except Exception as e:
                print(f"⚠ Advertencia: No se pudo actualizar en la base remota: {e}")
            finally:
                if cursor:
                    cursor.close()
                if conn:
                    conn.close()

        if callback:
            callback(True, None)

    Thread(target=db_task).start()

def initialize_database():
    """Crea la base de datos y las tablas necesarias si no existen."""
    try:

        # Ahora conectarse a la base de datos recién creada
        conn = local_pool.get_connection()
        cursor = conn.cursor()

        tables = {
            "control_diametro": """
                CREATE TABLE IF NOT EXISTS `control_diametro` (
                    `ID` INT NOT NULL AUTO_INCREMENT,
                    `colada` VARCHAR(45) NULL,
                    `crisol` VARCHAR(45) NULL,
                    `diametro_arriba` VARCHAR(45) NULL,
                    `diametro_medio` VARCHAR(45) NULL,
                    `diametro_abajo` VARCHAR(45) NULL,
                    `altura` VARCHAR(45) NULL,
                    `nivel_del_agua` VARCHAR(45) NULL,
                    `mangueras` VARCHAR(45) NULL,
                    `hidraulico` VARCHAR(45) NULL,
                    `limpieza_sector` VARCHAR(45) NULL,
                    PRIMARY KEY (`ID`)
                );
            """,
            "datos_sinterizado": """
                CREATE TABLE IF NOT EXISTS `datos_sinterizado` (
                    `ID` INT NOT NULL AUTO_INCREMENT,
                    `fecha` VARCHAR(45) NULL,
                    `hora` VARCHAR(45) NULL,
                    `temperatura_actual` VARCHAR(45) NULL,
                    `potencia_seteada` VARCHAR(45) NULL,
                    PRIMARY KEY (`ID`)
                );
            """,
            "cucharas_log": """
                CREATE TABLE IF NOT EXISTS `cucharas_log` (
                    `ID` INT NOT NULL AUTO_INCREMENT,
                    `colada` VARCHAR(45) NULL,
                    `cuchara` INT NULL,
                    `potencia` INT NULL,
                    `temperatura` FLOAT NULL,
                    `timestamp` DATETIME NULL,
                    PRIMARY KEY (`ID`)
                );
            """,
            "cucharas_por_material": """
                CREATE TABLE IF NOT EXISTS `cucharas_por_material` (
                    `ID` INT NOT NULL AUTO_INCREMENT,
                    `fecha` DATE NULL,
                    `colada` VARCHAR(45) NULL,
                    `base` VARCHAR(45) NULL,
                    `material_1` INT NULL,
                    `material_2` INT NULL,
                    `material_3` INT NULL,
                    `material_5` INT NULL,
                    `material_7` INT NULL,
                    `material_8` INT NULL,
                    `material_10` INT NULL,
                    `material_12` INT NULL,
                    PRIMARY KEY (`ID`)
                );
            """,
            "planilla_de_fusion": """
                CREATE TABLE IF NOT EXISTS `planilla_de_fusion` (
                    `ID` INT NOT NULL AUTO_INCREMENT,
                    `colada` VARCHAR(50) NULL,
                    `crisol` INT NULL,
                    `carga` VARCHAR(45) NULL,
                    `hora_inicio_carga` TIME NULL,
                    `carbono` VARCHAR(45) NULL,
                    `silicio` VARCHAR(45) NULL,
                    `acero_1010` VARCHAR(45) NULL,
                    `hora_inicio_de_colada` TIME NULL,
                    `hora_fin_de_colada` TIME NULL,
                    `fecha` DATE NULL,
                    PRIMARY KEY (`ID`)
                );
            """,
            "temperatura_potencia": """
                CREATE TABLE IF NOT EXISTS `temperatura_potencia` (
                    `ID` INT NOT NULL AUTO_INCREMENT,
                    `temperatura_thermocupla` FLOAT NULL,
                    `potencia_seteada` INT NULL,
                    PRIMARY KEY (`ID`)
                );
            """,
            "id_mapping": """
                CREATE TABLE IF NOT EXISTS `id_mapping` (
                    `local_id` INT NOT NULL,
                    `remote_id` INT NULL,
                    `tabla` VARCHAR(100) NOT NULL,
                    PRIMARY KEY (local_id, tabla)
                );
            """
            
        }
        
        for table_name, query in tables.items():
            cursor.execute(query)

        conn.commit()
        print("Base de datos local inicializada correctamente.")
    except Exception as e:
        print(f"Error al inicializar la base local: {e}")
    finally:
        if cursor:
            cursor.close()
        if conn:
            conn.close()

# Ejecutar la función al iniciar el programa
initialize_database()
sync_local_to_remote()