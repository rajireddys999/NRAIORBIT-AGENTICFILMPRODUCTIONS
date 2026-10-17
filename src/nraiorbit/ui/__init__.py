"""The control room (`nraiorbit ui`): one private web page over the bucket and the two queue sheets.

It reads what the runner writes (project backups, reports, the queues) and writes only to the queue sheets and the
stories folder; the runner does every paid step under its own caps. Behind Identity-Aware Proxy on Cloud Run, and
an allow-list of e-mail addresses inside the app as a second lock.
"""
