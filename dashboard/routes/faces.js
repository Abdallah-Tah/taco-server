import express from "express";
import multer from "multer";
import path from "path";
import fs from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import database from "better-sqlite3";
import { execSync } from "child_process";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const app = express.Router();

// Database setup
const DB_PATH = join(__dirname, "faces.db");
const KNOWN_FACES_DIR = "/home/abdaltm86/Documents/blink-cam/scripts/known_faces";
const UPLOAD_DIR = join(__dirname, "faces_uploads");

// Ensure directories exist
fs.mkdirSync(UPLOAD_DIR, { recursive: true });
fs.mkdirSync(KNOWN_FACES_DIR, { recursive: true });

const db = new database(DB_PATH);

// Create tables
db.exec(`
  CREATE TABLE IF NOT EXISTS persons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    image_path TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
  )
`);

// Configure multer for file uploads
const storage = multer.diskStorage({
  destination: (req, file, cb) => cb(null, UPLOAD_DIR),
  filename: (req, file, cb) => {
    const uniqueSuffix = Date.now() + "-" + Math.round(Math.random() * 1e9);
    cb(null, uniqueSuffix + path.extname(file.originalname));
  },
});

const upload = multer({
  storage,
  limits: { fileSize: 5 * 1024 * 1024 }, // 5MB
  fileFilter: (req, file, cb) => {
    const allowed = /jpeg|jpg|png/;
    const ext = allowed.test(path.extname(file.originalname).toLowerCase());
    const mime = allowed.test(file.mimetype);
    if (ext && mime) cb(null, true);
    else cb(new Error("Only JPEG and PNG images are allowed"));
  },
});

// Face image validation using face_recognition Python library
function validateFaceImage(imagePath) {
  try {
    const scriptPath = path.join(__dirname, '..', 'scripts', 'validate_face.py');
    const result = execSync(
      `"/home/abdaltm86/Documents/blink-cam/.venv311/bin/python3" "${scriptPath}" "${imagePath}"`,
      { timeout: 30000, encoding: 'utf-8' }
    );
    return JSON.parse(result.trim());
  } catch (e) {
    return { ok: false, error: "Face validation failed: " + e.message };
  }
}

// GET all persons
app.get("/", (req, res) => {
  try {
    const persons = db.prepare("SELECT * FROM persons ORDER BY created_at DESC").all();
    res.json(persons);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// GET single person
app.get("/:id", (req, res) => {
  try {
    const person = db.prepare("SELECT * FROM persons WHERE id = ?").get(req.params.id);
    if (!person) return res.status(404).json({ error: "Person not found" });
    res.json(person);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// POST new person (or re-add with same name - replaces old entry)
app.post("/", upload.single("image"), (req, res) => {
  try {
    const { name } = req.body;
    if (!name || !name.trim()) return res.status(400).json({ error: "Name is required" });
    if (!req.file) return res.status(400).json({ error: "Image is required" });

    const personName = name.trim();
    const imageFilename = req.file.filename;

    // Validate face quality
    const validation = validateFaceImage(req.file.path);
    if (!validation.ok) {
      // Remove uploaded file since validation failed
      if (fs.existsSync(req.file.path)) fs.unlinkSync(req.file.path);
      return res.status(400).json({ error: validation.error });
    }

    // Check if person with this name exists - delete old one completely
    const existing = db.prepare("SELECT * FROM persons WHERE LOWER(name) = LOWER(?)").get(personName);
    if (existing) {
      // Remove old files
      const oldUploadsPath = path.join(UPLOAD_DIR, existing.image_path);
      const oldKnownPath = path.join(KNOWN_FACES_DIR, `${existing.name}.jpg`);
      if (fs.existsSync(oldUploadsPath)) fs.unlinkSync(oldUploadsPath);
      if (fs.existsSync(oldKnownPath)) fs.unlinkSync(oldKnownPath);
      // Delete old record
      db.prepare("DELETE FROM persons WHERE id = ?").run(existing.id);
    }

    // Copy to known_faces directory
    const destPath = path.join(KNOWN_FACES_DIR, `${personName}.jpg`);
    fs.copyFileSync(req.file.path, destPath);

    // Save to database
    const result = db.prepare("INSERT INTO persons (name, image_path) VALUES (?, ?)").run(personName, imageFilename);

    res.json({
      id: result.lastInsertRowid,
      name: personName,
      image_path: imageFilename,
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// PUT update person (name and/or image)
app.put("/:id", upload.single("image"), (req, res) => {
  try {
    const { id } = req.params;
    const { name } = req.body;

    const person = db.prepare("SELECT * FROM persons WHERE id = ?").get(id);
    if (!person) return res.status(404).json({ error: "Person not found" });

    const oldName = person.name;
    const newName = name && name.trim() ? name.trim() : oldName;
    const newImageFilename = req.file ? req.file.filename : null;

    // Check if new name conflicts with another person
    if (newName.toLowerCase() !== oldName.toLowerCase()) {
      const conflict = db.prepare("SELECT * FROM persons WHERE LOWER(name) = LOWER(?) AND id != ?").get(newName, id);
      if (conflict) {
        return res.status(400).json({ error: "A person with this name already exists" });
      }
    }

    // Update name in database
    db.prepare("UPDATE persons SET name = ? WHERE id = ?").run(newName, id);

    // If new image provided, validate and update it
    if (newImageFilename) {
      // Validate face quality
      const validation = validateFaceImage(req.file.path);
      if (!validation.ok) {
        // Remove uploaded file since validation failed
        if (fs.existsSync(req.file.path)) fs.unlinkSync(req.file.path);
        return res.status(400).json({ error: validation.error });
      }

      // Copy new image to known_faces
      const destPath = path.join(KNOWN_FACES_DIR, `${newName}.jpg`);
      fs.copyFileSync(req.file.path, destPath);

      // Update image_path in database
      db.prepare("UPDATE persons SET image_path = ? WHERE id = ?").run(newImageFilename, id);

      // Remove old files
      const oldUploadsPath = path.join(UPLOAD_DIR, person.image_path);
      if (fs.existsSync(oldUploadsPath)) fs.unlinkSync(oldUploadsPath);
      if (oldName !== newName) {
        const oldKnownPath = path.join(KNOWN_FACES_DIR, `${oldName}.jpg`);
        if (fs.existsSync(oldKnownPath)) fs.unlinkSync(oldKnownPath);
      }
    } else if (oldName !== newName) {
      // Just rename the known_faces file
      const oldKnownPath = path.join(KNOWN_FACES_DIR, `${oldName}.jpg`);
      const newKnownPath = path.join(KNOWN_FACES_DIR, `${newName}.jpg`);
      if (fs.existsSync(oldKnownPath)) {
        fs.renameSync(oldKnownPath, newKnownPath);
      }
    }

    res.json({
      id: parseInt(id),
      name: newName,
      image_path: newImageFilename || person.image_path,
    });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// DELETE person
app.delete("/:id", (req, res) => {
  try {
    const person = db.prepare("SELECT * FROM persons WHERE id = ?").get(req.params.id);
    if (!person) return res.status(404).json({ error: "Person not found" });

    // Delete from database
    db.prepare("DELETE FROM persons WHERE id = ?").run(req.params.id);

    // Remove image files
    const uploadsPath = path.join(UPLOAD_DIR, person.image_path);
    const knownPath = path.join(KNOWN_FACES_DIR, `${person.name}.jpg`);
    if (fs.existsSync(uploadsPath)) fs.unlinkSync(uploadsPath);
    if (fs.existsSync(knownPath)) fs.unlinkSync(knownPath);

    res.json({ success: true });
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

// GET person image
app.get("/:id/image", (req, res) => {
  try {
    const person = db.prepare("SELECT * FROM persons WHERE id = ?").get(req.params.id);
    if (!person) return res.status(404).json({ error: "Person not found" });

    // First check uploads dir, then known_faces dir
    let imgPath = path.join(UPLOAD_DIR, person.image_path);
    if (!fs.existsSync(imgPath)) {
      imgPath = path.join(KNOWN_FACES_DIR, `${person.name}.jpg`);
    }
    if (!fs.existsSync(imgPath)) return res.status(404).json({ error: "Image not found" });

    res.sendFile(imgPath);
  } catch (e) {
    res.status(500).json({ error: e.message });
  }
});

export default app;
