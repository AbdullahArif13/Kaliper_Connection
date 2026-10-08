USE [master];
GO

IF DB_ID(N'qc') IS NULL
BEGIN
    CREATE DATABASE [qc];
END;
GO

USE [qc];
GO

IF OBJECT_ID(N'dbo.mold_mapping', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.mold_mapping
    (
        mold_number NVARCHAR(50) NOT NULL
            CONSTRAINT PK_mold_mapping PRIMARY KEY,
        tipe_grid NVARCHAR(50) NOT NULL
    );
END;
GO

DECLARE @line INT = 1;
DECLARE @table_name SYSNAME;
DECLARE @sql NVARCHAR(MAX);

WHILE @line <= 22
BEGIN
    SET @table_name = N'tbl_c' + RIGHT(N'0' + CONVERT(NVARCHAR(2), @line), 2);

    IF OBJECT_ID(N'dbo.' + @table_name, N'U') IS NULL
    BEGIN
        SET @sql = N'
            CREATE TABLE dbo.' + QUOTENAME(@table_name) + N'
            (
                id INT IDENTITY(1,1) NOT NULL
                    CONSTRAINT ' + QUOTENAME(N'PK_' + @table_name) + N' PRIMARY KEY,
                [timestamp] DATETIME NOT NULL,
                tipe NVARCHAR(50) NOT NULL,
                no_mold NVARCHAR(50) NOT NULL,
                sisi_a FLOAT NULL,
                sisi_b FLOAT NULL,
                sisi_c FLOAT NULL,
                sisi_d FLOAT NULL,
                sisi_e FLOAT NULL,
                sisi_f FLOAT NULL,
                sisi_g FLOAT NULL,
                sisi_h FLOAT NULL,
                [avg] FLOAT NULL
            );';

        EXEC sys.sp_executesql @sql;
    END;

    SET @line += 1;
END;
GO
