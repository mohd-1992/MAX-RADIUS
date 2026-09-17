-- ==========================================================
-- Standard FreeRADIUS 3.x Schema
-- Compatible with MySQL / MariaDB / SQLite
-- ==========================================================

CREATE TABLE IF NOT EXISTS nas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nasname VARCHAR(128) NOT NULL,
    shortname VARCHAR(32),
    type VARCHAR(30) DEFAULT 'other',
    ports INTEGER DEFAULT 1812,
    secret VARCHAR(60) NOT NULL,
    server VARCHAR(64),
    community VARCHAR(50),
    description VARCHAR(200) DEFAULT 'RADIUS Client'
);

CREATE TABLE IF NOT EXISTS radcheck (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(64) NOT NULL DEFAULT '',
    attribute VARCHAR(64) NOT NULL DEFAULT '',
    op CHAR(2) NOT NULL DEFAULT '==',
    value VARCHAR(253) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS radreply (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(64) NOT NULL DEFAULT '',
    attribute VARCHAR(64) NOT NULL DEFAULT '',
    op CHAR(2) NOT NULL DEFAULT '=',
    value VARCHAR(253) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS radgroupcheck (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    groupname VARCHAR(64) NOT NULL DEFAULT '',
    attribute VARCHAR(64) NOT NULL DEFAULT '',
    op CHAR(2) NOT NULL DEFAULT '==',
    value VARCHAR(253) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS radgroupreply (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    groupname VARCHAR(64) NOT NULL DEFAULT '',
    attribute VARCHAR(64) NOT NULL DEFAULT '',
    op CHAR(2) NOT NULL DEFAULT '=',
    value VARCHAR(253) NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS radusergroup (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(64) NOT NULL DEFAULT '',
    groupname VARCHAR(64) NOT NULL DEFAULT '',
    priority INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS radacct (
    radacctid INTEGER PRIMARY KEY AUTOINCREMENT,
    acctsessionid VARCHAR(64) NOT NULL DEFAULT '',
    acctuniqueid VARCHAR(32) NOT NULL DEFAULT '',
    username VARCHAR(64) NOT NULL DEFAULT '',
    realm VARCHAR(64) DEFAULT '',
    nasipaddress VARCHAR(45) NOT NULL DEFAULT '',
    nasportid VARCHAR(32) DEFAULT NULL,
    nasporttype VARCHAR(32) DEFAULT NULL,
    acctstarttime DATETIME DEFAULT CURRENT_TIMESTAMP,
    acctupdatetime DATETIME DEFAULT NULL,
    acctstoptime DATETIME DEFAULT NULL,
    acctinterval INTEGER DEFAULT 60,
    acctsessiontime INTEGER DEFAULT 0,
    acctauthentic VARCHAR(32) DEFAULT NULL,
    connectinfo_start VARCHAR(50) DEFAULT NULL,
    connectinfo_stop VARCHAR(50) DEFAULT NULL,
    acctinputoctets BIGINT DEFAULT 0,
    acctoutputoctets BIGINT DEFAULT 0,
    calledstationid VARCHAR(50) NOT NULL DEFAULT '',
    callingstationid VARCHAR(50) NOT NULL DEFAULT '',
    acctterminatecause VARCHAR(32) NOT NULL DEFAULT '',
    servicetype VARCHAR(32) DEFAULT NULL,
    framedprotocol VARCHAR(32) DEFAULT NULL,
    framedipaddress VARCHAR(45) NOT NULL DEFAULT '',
    framedipv6address VARCHAR(45) NOT NULL DEFAULT '',
    framedipv6prefix VARCHAR(45) NOT NULL DEFAULT '',
    framedinterfaceid VARCHAR(44) NOT NULL DEFAULT '',
    delegatedipv6prefix VARCHAR(45) NOT NULL DEFAULT ''
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_radacct_session_nas ON radacct(nasipaddress, acctsessionid);
CREATE INDEX IF NOT EXISTS idx_radacct_user ON radacct(username);
CREATE INDEX IF NOT EXISTS idx_radacct_active ON radacct(acctstoptime);

CREATE TABLE IF NOT EXISTS radpostauth (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username VARCHAR(64) NOT NULL DEFAULT '',
    pass VARCHAR(64) NOT NULL DEFAULT '',
    reply VARCHAR(32) NOT NULL DEFAULT '',
    authdate DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    class VARCHAR(64) DEFAULT NULL
);
