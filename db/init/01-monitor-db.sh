#!/bin/bash
# Runs once, when the MariaDB volume is first initialised.
# Creates the monitoring database and a user that may also READ Nextcloud's tables.
set -e

mariadb --protocol=socket -uroot -p"${MARIADB_ROOT_PASSWORD}" <<EOSQL
CREATE DATABASE IF NOT EXISTS monitor CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci;
CREATE USER IF NOT EXISTS 'monitor'@'%' IDENTIFIED BY '${MONITOR_DB_PASSWORD}';
GRANT ALL PRIVILEGES ON monitor.* TO 'monitor'@'%';
GRANT SELECT ON nextcloud.* TO 'monitor'@'%';
FLUSH PRIVILEGES;
EOSQL
