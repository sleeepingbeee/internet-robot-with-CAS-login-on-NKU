**1.dify去除历史活动**
```sql
DELETE FROM activities
WHERE substr(活动时间, 1, 10) < substr('{{#1792804399381.text#}}', 1, 10);
```

**2.powershell查询全部数据**
```shell
& 'D:\sqlite\sqlite_tools\sqlite3.exe' -cmd ".mode line" 'D:\sqlite\dify_sqlite\activities.db' "SELECT * FROM activities;"
```

**3.dify查询数据库全部数据**
```sql
SELECT * FROM activities ORDER BY 活动时间 ASC;
```