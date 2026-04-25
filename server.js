const express = require('express');
const Database = require('better-sqlite3');
const { v4: uuidv4 } = require('uuid');
const path = require('path');

const app = express();
const db = new Database('travel.db');

app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

// Init DB
db.exec(`
  CREATE TABLE IF NOT EXISTS trips (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    destination TEXT,
    created_at INTEGER DEFAULT (strftime('%s', 'now'))
  );

  CREATE TABLE IF NOT EXISTS members (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL,
    name TEXT NOT NULL,
    color TEXT NOT NULL,
    FOREIGN KEY (trip_id) REFERENCES trips(id)
  );

  CREATE TABLE IF NOT EXISTS expenses (
    id TEXT PRIMARY KEY,
    trip_id TEXT NOT NULL,
    name TEXT NOT NULL,
    total_amount REAL NOT NULL,
    paid_by TEXT,
    created_at INTEGER DEFAULT (strftime('%s', 'now')),
    FOREIGN KEY (trip_id) REFERENCES trips(id)
  );

  CREATE TABLE IF NOT EXISTS expense_splits (
    id TEXT PRIMARY KEY,
    expense_id TEXT NOT NULL,
    member_id TEXT NOT NULL,
    amount REAL NOT NULL,
    paid INTEGER DEFAULT 0,
    FOREIGN KEY (expense_id) REFERENCES expenses(id),
    FOREIGN KEY (member_id) REFERENCES members(id)
  );
`);

// Create trip
app.post('/api/trips', (req, res) => {
  const { name, destination } = req.body;
  if (!name) return res.status(400).json({ error: 'Trip name required' });
  const id = uuidv4().slice(0, 8);
  db.prepare('INSERT INTO trips (id, name, destination) VALUES (?, ?, ?)').run(id, name, destination || '');
  res.json({ id, name, destination });
});

// Get trip
app.get('/api/trips/:id', (req, res) => {
  const trip = db.prepare('SELECT * FROM trips WHERE id = ?').get(req.params.id);
  if (!trip) return res.status(404).json({ error: 'Trip not found' });

  const members = db.prepare('SELECT * FROM members WHERE trip_id = ?').all(trip.id);
  const expenses = db.prepare('SELECT * FROM expenses WHERE trip_id = ? ORDER BY created_at DESC').all(trip.id);

  const expensesWithSplits = expenses.map(exp => {
    const splits = db.prepare(`
      SELECT es.*, m.name as member_name, m.color as member_color
      FROM expense_splits es
      JOIN members m ON es.member_id = m.id
      WHERE es.expense_id = ?
    `).all(exp.id);
    return { ...exp, splits };
  });

  res.json({ ...trip, members, expenses: expensesWithSplits });
});

// Add member
app.post('/api/trips/:id/members', (req, res) => {
  const { name } = req.body;
  if (!name) return res.status(400).json({ error: 'Member name required' });

  const trip = db.prepare('SELECT id FROM trips WHERE id = ?').get(req.params.id);
  if (!trip) return res.status(404).json({ error: 'Trip not found' });

  const colors = ['#FF6B6B','#4ECDC4','#45B7D1','#96CEB4','#FFEAA7','#DDA0DD','#98D8C8','#F7DC6F','#BB8FCE','#85C1E9'];
  const existingCount = db.prepare('SELECT COUNT(*) as c FROM members WHERE trip_id = ?').get(trip.id).c;
  const color = colors[existingCount % colors.length];

  const id = uuidv4();
  db.prepare('INSERT INTO members (id, trip_id, name, color) VALUES (?, ?, ?, ?)').run(id, trip.id, name, color);

  // Add this member to all existing expenses with equal split
  const expenses = db.prepare('SELECT * FROM expenses WHERE trip_id = ?').all(trip.id);
  for (const exp of expenses) {
    const currentSplits = db.prepare('SELECT COUNT(*) as c FROM expense_splits WHERE expense_id = ?').get(exp.id).c;
    const newCount = currentSplits + 1;
    const equalShare = exp.total_amount / newCount;

    // Update existing splits
    db.prepare('UPDATE expense_splits SET amount = ? WHERE expense_id = ?').run(equalShare, exp.id);

    // Add new split
    db.prepare('INSERT INTO expense_splits (id, expense_id, member_id, amount, paid) VALUES (?, ?, ?, ?, 0)')
      .run(uuidv4(), exp.id, id, equalShare);
  }

  res.json({ id, name, color });
});

// Add expense
app.post('/api/trips/:id/expenses', (req, res) => {
  const { name, total_amount, paid_by, splits } = req.body;
  if (!name || !total_amount) return res.status(400).json({ error: 'Name and amount required' });

  const trip = db.prepare('SELECT id FROM trips WHERE id = ?').get(req.params.id);
  if (!trip) return res.status(404).json({ error: 'Trip not found' });

  const expId = uuidv4();
  db.prepare('INSERT INTO expenses (id, trip_id, name, total_amount, paid_by) VALUES (?, ?, ?, ?, ?)')
    .run(expId, trip.id, name, total_amount, paid_by || null);

  const members = db.prepare('SELECT * FROM members WHERE trip_id = ?').all(trip.id);

  if (splits && splits.length > 0) {
    for (const split of splits) {
      db.prepare('INSERT INTO expense_splits (id, expense_id, member_id, amount, paid) VALUES (?, ?, ?, ?, ?)')
        .run(uuidv4(), expId, split.member_id, split.amount, split.paid ? 1 : 0);
    }
  } else if (members.length > 0) {
    const equalShare = total_amount / members.length;
    for (const member of members) {
      const isPaid = member.id === paid_by ? 1 : 0;
      db.prepare('INSERT INTO expense_splits (id, expense_id, member_id, amount, paid) VALUES (?, ?, ?, ?, ?)')
        .run(uuidv4(), expId, member.id, equalShare, isPaid);
    }
  }

  res.json({ id: expId });
});

// Delete expense
app.delete('/api/expenses/:id', (req, res) => {
  db.prepare('DELETE FROM expense_splits WHERE expense_id = ?').run(req.params.id);
  db.prepare('DELETE FROM expenses WHERE id = ?').run(req.params.id);
  res.json({ ok: true });
});

// Toggle payment status
app.patch('/api/splits/:id/toggle', (req, res) => {
  const split = db.prepare('SELECT * FROM expense_splits WHERE id = ?').get(req.params.id);
  if (!split) return res.status(404).json({ error: 'Not found' });
  const newPaid = split.paid ? 0 : 1;
  db.prepare('UPDATE expense_splits SET paid = ? WHERE id = ?').run(newPaid, split.id);
  res.json({ paid: newPaid === 1 });
});

// Update split amount
app.patch('/api/splits/:id', (req, res) => {
  const { amount, paid } = req.body;
  const split = db.prepare('SELECT * FROM expense_splits WHERE id = ?').get(req.params.id);
  if (!split) return res.status(404).json({ error: 'Not found' });
  db.prepare('UPDATE expense_splits SET amount = ?, paid = ? WHERE id = ?')
    .run(amount ?? split.amount, paid !== undefined ? (paid ? 1 : 0) : split.paid, split.id);
  res.json({ ok: true });
});

// Serve frontend for all other routes
app.get('*', (req, res) => {
  res.sendFile(path.join(__dirname, 'public', 'index.html'));
});

const PORT = process.env.PORT || 3000;
app.listen(PORT, () => console.log(`Travel app running at http://localhost:${PORT}`));
