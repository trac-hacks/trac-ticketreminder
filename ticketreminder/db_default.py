from trac.db import Table, Column, Index

version = 1
name = 'ticketreminder'

schema = [
	Table('ticketreminder', key='id')[
		Column('id', auto_increment=True),
		Column('ticket', type='int'),
        Column('time', type='int64'),
        Column('author'),
        Column('origin', type='int64'),
        Column('reminded', type='int'),
        Column('repeat', type='int'),
        Column('description'),
        Index(['ticket']),
        Index(['time'])],
]
