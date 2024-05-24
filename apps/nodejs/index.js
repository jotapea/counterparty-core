
const express = require('express');
const cors = require('cors');

// // read only
// import sqlite3 from 'better-sqlite3';
// const DB_PATH = '/data/counterparty/counterparty.db';
// const db = sqlite3(DB_PATH, { readonly: true });
// // ... (see: https://github.com/CNTRPRTY/xcpdev-api)

const app = express();
app.use(cors());
const port = 3001;

app.get('/', async (req, res) => {
    res.status(200).json({
        online: true
    });
});

app.listen(port, () => {
    console.log(`Example app listening on port ${port}`);
});
