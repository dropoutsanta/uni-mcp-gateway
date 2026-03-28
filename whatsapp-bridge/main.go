package main

import (
	"context"
	cryptorand "crypto/rand"
	"database/sql"
	"encoding/base64"
	"encoding/binary"
	"encoding/json"
	"fmt"
	"math"
	"math/rand"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"reflect"
	"strconv"
	"strings"
	"syscall"
	"time"

	_ "github.com/mattn/go-sqlite3"
	"github.com/mdp/qrterminal"

	"bytes"

	"go.mau.fi/whatsmeow"
	waProto "go.mau.fi/whatsmeow/binary/proto"
	waE2E "go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/proto/waCompanionReg"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"
	"google.golang.org/protobuf/proto"
)

// Message represents a chat message for our client
type Message struct {
	Time      time.Time
	Sender    string
	Content   string
	IsFromMe  bool
	MediaType string
	Filename  string
}

// Database handler for storing message history
type MessageStore struct {
	db *sql.DB
}

// Initialize message store
func NewMessageStore() (*MessageStore, error) {
	// Create directory for database if it doesn't exist
	if err := os.MkdirAll("store", 0755); err != nil {
		return nil, fmt.Errorf("failed to create store directory: %v", err)
	}

	// Open SQLite database for messages
	db, err := sql.Open("sqlite3", "file:store/messages.db?_foreign_keys=on")
	if err != nil {
		return nil, fmt.Errorf("failed to open message database: %v", err)
	}

	// Create tables if they don't exist
	_, err = db.Exec(`
		CREATE TABLE IF NOT EXISTS chats (
			jid TEXT PRIMARY KEY,
			name TEXT,
			last_message_time TIMESTAMP
		);
		
		CREATE TABLE IF NOT EXISTS messages (
			id TEXT,
			chat_jid TEXT,
			sender TEXT,
			content TEXT,
			timestamp TIMESTAMP,
			is_from_me BOOLEAN,
			media_type TEXT,
			filename TEXT,
			url TEXT,
			media_key BLOB,
			file_sha256 BLOB,
			file_enc_sha256 BLOB,
			file_length INTEGER,
			PRIMARY KEY (id, chat_jid),
			FOREIGN KEY (chat_jid) REFERENCES chats(jid)
		);

		CREATE TABLE IF NOT EXISTS contacts (
			jid TEXT PRIMARY KEY,
			phone TEXT,
			full_name TEXT,
			push_name TEXT,
			business_name TEXT,
			updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
		);

		CREATE TABLE IF NOT EXISTS group_members (
			group_jid TEXT,
			member_jid TEXT,
			display_name TEXT,
			is_admin BOOLEAN DEFAULT 0,
			is_super_admin BOOLEAN DEFAULT 0,
			PRIMARY KEY (group_jid, member_jid)
		);

		CREATE TABLE IF NOT EXISTS access_keys (
			id TEXT PRIMARY KEY,
			api_key TEXT UNIQUE NOT NULL,
			label TEXT DEFAULT '',
			scope_all BOOLEAN DEFAULT 0,
			created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
		);

		CREATE TABLE IF NOT EXISTS access_key_scopes (
			key_id TEXT NOT NULL,
			chat_jid TEXT NOT NULL,
			PRIMARY KEY (key_id, chat_jid),
			FOREIGN KEY (key_id) REFERENCES access_keys(id) ON DELETE CASCADE
		);
	`)
	if err != nil {
		db.Close()
		return nil, fmt.Errorf("failed to create tables: %v", err)
	}

	return &MessageStore{db: db}, nil
}

// Close the database connection
func (store *MessageStore) Close() error {
	return store.db.Close()
}

// Store a chat in the database
func (store *MessageStore) StoreChat(jid, name string, lastMessageTime time.Time) error {
	_, err := store.db.Exec(
		"INSERT OR REPLACE INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)",
		jid, name, lastMessageTime,
	)
	return err
}

// Store a message in the database
func (store *MessageStore) StoreMessage(id, chatJID, sender, content string, timestamp time.Time, isFromMe bool,
	mediaType, filename, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	// Only store if there's actual content or media
	if content == "" && mediaType == "" {
		return nil
	}

	_, err := store.db.Exec(
		`INSERT OR REPLACE INTO messages 
		(id, chat_jid, sender, content, timestamp, is_from_me, media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length) 
		VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		id, chatJID, sender, content, timestamp, isFromMe, mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength,
	)
	return err
}

// Get messages from a chat
func (store *MessageStore) GetMessages(chatJID string, limit int) ([]Message, error) {
	rows, err := store.db.Query(
		"SELECT sender, content, timestamp, is_from_me, media_type, filename FROM messages WHERE chat_jid = ? ORDER BY timestamp DESC LIMIT ?",
		chatJID, limit,
	)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var messages []Message
	for rows.Next() {
		var msg Message
		var timestamp time.Time
		err := rows.Scan(&msg.Sender, &msg.Content, &timestamp, &msg.IsFromMe, &msg.MediaType, &msg.Filename)
		if err != nil {
			return nil, err
		}
		msg.Time = timestamp
		messages = append(messages, msg)
	}

	return messages, nil
}

// Get all chats
func (store *MessageStore) GetChats() (map[string]time.Time, error) {
	rows, err := store.db.Query("SELECT jid, last_message_time FROM chats ORDER BY last_message_time DESC")
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	chats := make(map[string]time.Time)
	for rows.Next() {
		var jid string
		var lastMessageTime time.Time
		err := rows.Scan(&jid, &lastMessageTime)
		if err != nil {
			return nil, err
		}
		chats[jid] = lastMessageTime
	}

	return chats, nil
}

// Extract text content from a message
func extractTextContent(msg *waProto.Message) string {
	if msg == nil {
		return ""
	}

	// Try to get text content
	if text := msg.GetConversation(); text != "" {
		return text
	} else if extendedText := msg.GetExtendedTextMessage(); extendedText != nil {
		return extendedText.GetText()
	}

	// For now, we're ignoring non-text messages
	return ""
}

// SendMessageResponse represents the response for the send message API
type SendMessageResponse struct {
	Success bool   `json:"success"`
	Message string `json:"message"`
}

// SendMessageRequest represents the request body for the send message API
type SendMessageRequest struct {
	Recipient string `json:"recipient"`
	Message   string `json:"message"`
	MediaPath string `json:"media_path,omitempty"`
}

// Function to send a WhatsApp message
func sendWhatsAppMessage(client *whatsmeow.Client, recipient string, message string, mediaPath string) (bool, string) {
	if !client.IsConnected() {
		return false, "Not connected to WhatsApp"
	}

	// Create JID for recipient
	var recipientJID types.JID
	var err error

	// Check if recipient is a JID
	isJID := strings.Contains(recipient, "@")

	if isJID {
		// Parse the JID string
		recipientJID, err = types.ParseJID(recipient)
		if err != nil {
			return false, fmt.Sprintf("Error parsing JID: %v", err)
		}
	} else {
		// Resolve phone number via IsOnWhatsApp to get the correct LID-aware JID
		resp, err := client.IsOnWhatsApp(context.Background(), []string{"+" + recipient})
		if err == nil && len(resp) > 0 && resp[0].IsIn {
			recipientJID = resp[0].JID
		} else {
			recipientJID = types.JID{
				User:   recipient,
				Server: "s.whatsapp.net",
			}
		}
	}

	msg := &waProto.Message{}

	// Check if we have media to send
	if mediaPath != "" {
		// Read media file
		mediaData, err := os.ReadFile(mediaPath)
		if err != nil {
			return false, fmt.Sprintf("Error reading media file: %v", err)
		}

		// Determine media type and mime type based on file extension
		fileExt := strings.ToLower(mediaPath[strings.LastIndex(mediaPath, ".")+1:])
		var mediaType whatsmeow.MediaType
		var mimeType string

		// Handle different media types
		switch fileExt {
		// Image types
		case "jpg", "jpeg":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/jpeg"
		case "png":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/png"
		case "gif":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/gif"
		case "webp":
			mediaType = whatsmeow.MediaImage
			mimeType = "image/webp"

		// Audio types
		case "ogg":
			mediaType = whatsmeow.MediaAudio
			mimeType = "audio/ogg; codecs=opus"

		// Video types
		case "mp4":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/mp4"
		case "avi":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/avi"
		case "mov":
			mediaType = whatsmeow.MediaVideo
			mimeType = "video/quicktime"

		// Document types (for any other file type)
		default:
			mediaType = whatsmeow.MediaDocument
			mimeType = "application/octet-stream"
		}

		// Upload media to WhatsApp servers
		resp, err := client.Upload(context.Background(), mediaData, mediaType)
		if err != nil {
			return false, fmt.Sprintf("Error uploading media: %v", err)
		}

		fmt.Println("Media uploaded", resp)

		// Create the appropriate message type based on media type
		switch mediaType {
		case whatsmeow.MediaImage:
			msg.ImageMessage = &waProto.ImageMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaAudio:
			// Handle ogg audio files
			var seconds uint32 = 30 // Default fallback
			var waveform []byte = nil

			// Try to analyze the ogg file
			if strings.Contains(mimeType, "ogg") {
				analyzedSeconds, analyzedWaveform, err := analyzeOggOpus(mediaData)
				if err == nil {
					seconds = analyzedSeconds
					waveform = analyzedWaveform
				} else {
					return false, fmt.Sprintf("Failed to analyze Ogg Opus file: %v", err)
				}
			} else {
				fmt.Printf("Not an Ogg Opus file: %s\n", mimeType)
			}

			msg.AudioMessage = &waProto.AudioMessage{
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
				Seconds:       proto.Uint32(seconds),
				PTT:           proto.Bool(true),
				Waveform:      waveform,
			}
		case whatsmeow.MediaVideo:
			msg.VideoMessage = &waProto.VideoMessage{
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		case whatsmeow.MediaDocument:
			msg.DocumentMessage = &waProto.DocumentMessage{
				Title:         proto.String(mediaPath[strings.LastIndex(mediaPath, "/")+1:]),
				Caption:       proto.String(message),
				Mimetype:      proto.String(mimeType),
				URL:           &resp.URL,
				DirectPath:    &resp.DirectPath,
				MediaKey:      resp.MediaKey,
				FileEncSHA256: resp.FileEncSHA256,
				FileSHA256:    resp.FileSHA256,
				FileLength:    &resp.FileLength,
			}
		}
	} else {
		msg.Conversation = proto.String(message)
	}

	// Send message — retry without LID if the recipient hasn't been migrated yet
	_, err = client.SendMessage(context.Background(), recipientJID, msg)

	if err != nil && strings.Contains(err.Error(), "no LID found") {
		origTS := client.Store.LIDMigrationTimestamp
		client.Store.LIDMigrationTimestamp = 0
		_, err = client.SendMessage(context.Background(), recipientJID, msg)
		client.Store.LIDMigrationTimestamp = origTS
	}

	if err != nil {
		return false, fmt.Sprintf("Error sending message: %v", err)
	}

	return true, fmt.Sprintf("Message sent to %s", recipient)
}

// Extract media info from a message
func extractMediaInfo(msg *waProto.Message) (mediaType string, filename string, url string, mediaKey []byte, fileSHA256 []byte, fileEncSHA256 []byte, fileLength uint64) {
	if msg == nil {
		return "", "", "", nil, nil, nil, 0
	}

	// Check for image message
	if img := msg.GetImageMessage(); img != nil {
		return "image", "image_" + time.Now().Format("20060102_150405") + ".jpg",
			img.GetURL(), img.GetMediaKey(), img.GetFileSHA256(), img.GetFileEncSHA256(), img.GetFileLength()
	}

	// Check for video message
	if vid := msg.GetVideoMessage(); vid != nil {
		return "video", "video_" + time.Now().Format("20060102_150405") + ".mp4",
			vid.GetURL(), vid.GetMediaKey(), vid.GetFileSHA256(), vid.GetFileEncSHA256(), vid.GetFileLength()
	}

	// Check for audio message
	if aud := msg.GetAudioMessage(); aud != nil {
		return "audio", "audio_" + time.Now().Format("20060102_150405") + ".ogg",
			aud.GetURL(), aud.GetMediaKey(), aud.GetFileSHA256(), aud.GetFileEncSHA256(), aud.GetFileLength()
	}

	// Check for document message
	if doc := msg.GetDocumentMessage(); doc != nil {
		filename := doc.GetFileName()
		if filename == "" {
			filename = "document_" + time.Now().Format("20060102_150405")
		}
		return "document", filename,
			doc.GetURL(), doc.GetMediaKey(), doc.GetFileSHA256(), doc.GetFileEncSHA256(), doc.GetFileLength()
	}

	return "", "", "", nil, nil, nil, 0
}

// Handle regular incoming messages with media support
// resolveLID converts a LID-format JID to a phone number JID if a mapping exists.
// Returns the original JID string unchanged if it's not a LID or no mapping is found.
func resolveLID(client *whatsmeow.Client, jid types.JID, logger waLog.Logger) (types.JID, string) {
	jidStr := jid.String()
	if jid.Server != "lid" {
		return jid, jidStr
	}
	pnJID, err := client.Store.LIDs.GetPNForLID(context.Background(), jid)
	if err != nil || pnJID.IsEmpty() || pnJID.User == "" {
		return jid, jidStr
	}
	logger.Infof("Resolved LID %s -> %s", jidStr, pnJID.String())
	return pnJID, pnJID.String()
}

// resolveLIDUser converts a LID user identifier to a phone number if possible.
func resolveLIDUser(client *whatsmeow.Client, user string, server string, logger waLog.Logger) string {
	if server != "lid" && !strings.HasSuffix(user, "@lid") {
		return user
	}
	lidJID := types.NewJID(user, "lid")
	pnJID, err := client.Store.LIDs.GetPNForLID(context.Background(), lidJID)
	if err != nil || pnJID.IsEmpty() || pnJID.User == "" {
		return user
	}
	return pnJID.User
}

func handleMessage(client *whatsmeow.Client, messageStore *MessageStore, msg *events.Message, logger waLog.Logger) {
	// Resolve LID to phone number JID if needed
	chatParsed, chatJID := resolveLID(client, msg.Info.Chat, logger)
	_ = chatParsed
	sender := msg.Info.Sender.User
	if msg.Info.Sender.Server == "lid" {
		sender = resolveLIDUser(client, sender, "lid", logger)
	}

	// Get appropriate chat name (pass nil for conversation since we don't have one for regular messages)
	name := GetChatName(client, messageStore, msg.Info.Chat, chatJID, nil, sender, logger)

	// Update chat in database with the message timestamp (keeps last message time updated)
	err := messageStore.StoreChat(chatJID, name, msg.Info.Timestamp)
	if err != nil {
		logger.Warnf("Failed to store chat: %v", err)
	}

	// Extract text content
	content := extractTextContent(msg.Message)

	// Extract media info
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength := extractMediaInfo(msg.Message)

	// Skip if there's no content and no media
	if content == "" && mediaType == "" {
		return
	}

	// Store message in database
	err = messageStore.StoreMessage(
		msg.Info.ID,
		chatJID,
		sender,
		content,
		msg.Info.Timestamp,
		msg.Info.IsFromMe,
		mediaType,
		filename,
		url,
		mediaKey,
		fileSHA256,
		fileEncSHA256,
		fileLength,
	)

	if err != nil {
		logger.Warnf("Failed to store message: %v", err)
	} else {
		// Log message reception
		timestamp := msg.Info.Timestamp.Format("2006-01-02 15:04:05")
		direction := "←"
		if msg.Info.IsFromMe {
			direction = "→"
		}

		// Log based on message type
		if mediaType != "" {
			fmt.Printf("[%s] %s %s: [%s: %s] %s\n", timestamp, direction, sender, mediaType, filename, content)
		} else if content != "" {
			fmt.Printf("[%s] %s %s: %s\n", timestamp, direction, sender, content)
		}
	}
}

// DownloadMediaRequest represents the request body for the download media API
type DownloadMediaRequest struct {
	MessageID string `json:"message_id"`
	ChatJID   string `json:"chat_jid"`
}

// DownloadMediaResponse represents the response for the download media API
type DownloadMediaResponse struct {
	Success  bool   `json:"success"`
	Message  string `json:"message"`
	Filename string `json:"filename,omitempty"`
	Path     string `json:"path,omitempty"`
}

// Store additional media info in the database
func (store *MessageStore) StoreMediaInfo(id, chatJID, url string, mediaKey, fileSHA256, fileEncSHA256 []byte, fileLength uint64) error {
	_, err := store.db.Exec(
		"UPDATE messages SET url = ?, media_key = ?, file_sha256 = ?, file_enc_sha256 = ?, file_length = ? WHERE id = ? AND chat_jid = ?",
		url, mediaKey, fileSHA256, fileEncSHA256, fileLength, id, chatJID,
	)
	return err
}

// Get media info from the database
func (store *MessageStore) GetMediaInfo(id, chatJID string) (string, string, string, []byte, []byte, []byte, uint64, error) {
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64

	err := store.db.QueryRow(
		"SELECT media_type, filename, url, media_key, file_sha256, file_enc_sha256, file_length FROM messages WHERE id = ? AND chat_jid = ?",
		id, chatJID,
	).Scan(&mediaType, &filename, &url, &mediaKey, &fileSHA256, &fileEncSHA256, &fileLength)

	return mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err
}

// MediaDownloader implements the whatsmeow.DownloadableMessage interface
type MediaDownloader struct {
	URL           string
	DirectPath    string
	MediaKey      []byte
	FileLength    uint64
	FileSHA256    []byte
	FileEncSHA256 []byte
	MediaType     whatsmeow.MediaType
}

// GetDirectPath implements the DownloadableMessage interface
func (d *MediaDownloader) GetDirectPath() string {
	return d.DirectPath
}

// GetURL implements the DownloadableMessage interface
func (d *MediaDownloader) GetURL() string {
	return d.URL
}

// GetMediaKey implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaKey() []byte {
	return d.MediaKey
}

// GetFileLength implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileLength() uint64 {
	return d.FileLength
}

// GetFileSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileSHA256() []byte {
	return d.FileSHA256
}

// GetFileEncSHA256 implements the DownloadableMessage interface
func (d *MediaDownloader) GetFileEncSHA256() []byte {
	return d.FileEncSHA256
}

// GetMediaType implements the DownloadableMessage interface
func (d *MediaDownloader) GetMediaType() whatsmeow.MediaType {
	return d.MediaType
}

// Function to download media from a message
func downloadMedia(client *whatsmeow.Client, messageStore *MessageStore, messageID, chatJID string) (bool, string, string, string, error) {
	// Query the database for the message
	var mediaType, filename, url string
	var mediaKey, fileSHA256, fileEncSHA256 []byte
	var fileLength uint64
	var err error

	// First, check if we already have this file
	chatDir := fmt.Sprintf("store/%s", strings.ReplaceAll(chatJID, ":", "_"))
	localPath := ""

	// Get media info from the database
	mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength, err = messageStore.GetMediaInfo(messageID, chatJID)

	if err != nil {
		// Try to get basic info if extended info isn't available
		err = messageStore.db.QueryRow(
			"SELECT media_type, filename FROM messages WHERE id = ? AND chat_jid = ?",
			messageID, chatJID,
		).Scan(&mediaType, &filename)

		if err != nil {
			return false, "", "", "", fmt.Errorf("failed to find message: %v", err)
		}
	}

	// Check if this is a media message
	if mediaType == "" {
		return false, "", "", "", fmt.Errorf("not a media message")
	}

	// Create directory for the chat if it doesn't exist
	if err := os.MkdirAll(chatDir, 0755); err != nil {
		return false, "", "", "", fmt.Errorf("failed to create chat directory: %v", err)
	}

	// Generate a local path for the file
	localPath = fmt.Sprintf("%s/%s", chatDir, filename)

	// Get absolute path
	absPath, err := filepath.Abs(localPath)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to get absolute path: %v", err)
	}

	// Check if file already exists
	if _, err := os.Stat(localPath); err == nil {
		// File exists, return it
		return true, mediaType, filename, absPath, nil
	}

	// If we don't have all the media info we need, we can't download
	if url == "" || len(mediaKey) == 0 || len(fileSHA256) == 0 || len(fileEncSHA256) == 0 || fileLength == 0 {
		return false, "", "", "", fmt.Errorf("incomplete media information for download")
	}

	fmt.Printf("Attempting to download media for message %s in chat %s...\n", messageID, chatJID)

	// Extract direct path from URL
	directPath := extractDirectPathFromURL(url)

	// Create a downloader that implements DownloadableMessage
	var waMediaType whatsmeow.MediaType
	switch mediaType {
	case "image":
		waMediaType = whatsmeow.MediaImage
	case "video":
		waMediaType = whatsmeow.MediaVideo
	case "audio":
		waMediaType = whatsmeow.MediaAudio
	case "document":
		waMediaType = whatsmeow.MediaDocument
	default:
		return false, "", "", "", fmt.Errorf("unsupported media type: %s", mediaType)
	}

	downloader := &MediaDownloader{
		URL:           url,
		DirectPath:    directPath,
		MediaKey:      mediaKey,
		FileLength:    fileLength,
		FileSHA256:    fileSHA256,
		FileEncSHA256: fileEncSHA256,
		MediaType:     waMediaType,
	}

	// Download the media using whatsmeow client
	mediaData, err := client.Download(context.Background(), downloader)
	if err != nil {
		return false, "", "", "", fmt.Errorf("failed to download media: %v", err)
	}

	// Save the downloaded media to file
	if err := os.WriteFile(localPath, mediaData, 0644); err != nil {
		return false, "", "", "", fmt.Errorf("failed to save media file: %v", err)
	}

	fmt.Printf("Successfully downloaded %s media to %s (%d bytes)\n", mediaType, absPath, len(mediaData))
	return true, mediaType, filename, absPath, nil
}

// Extract direct path from a WhatsApp media URL
func extractDirectPathFromURL(url string) string {
	// The direct path is typically in the URL, we need to extract it
	// Example URL: https://mmg.whatsapp.net/v/t62.7118-24/13812002_698058036224062_3424455886509161511_n.enc?ccb=11-4&oh=...

	// Find the path part after the domain
	parts := strings.SplitN(url, ".net/", 2)
	if len(parts) < 2 {
		return url // Return original URL if parsing fails
	}

	pathPart := parts[1]

	// Remove query parameters
	pathPart = strings.SplitN(pathPart, "?", 2)[0]

	// Create proper direct path format
	return "/" + pathPart
}

func generateAPIKey() string {
	b := make([]byte, 32)
	cryptorand.Read(b)
	return base64.URLEncoding.EncodeToString(b)
}

// Middleware to verify API key from WHATSAPP_BRIDGE_API_KEY env var.
// If the env var is not set, all requests are allowed (local dev).
func requireAPIKey(next http.HandlerFunc) http.HandlerFunc {
	apiKey := os.Getenv("WHATSAPP_BRIDGE_API_KEY")
	return func(w http.ResponseWriter, r *http.Request) {
		if apiKey != "" {
			auth := r.Header.Get("Authorization")
			if auth != "Bearer "+apiKey {
				http.Error(w, `{"error":"unauthorized"}`, http.StatusUnauthorized)
				return
			}
		}
		next(w, r)
	}
}

// Start a REST API server to expose the WhatsApp client functionality
func startRESTServer(client *whatsmeow.Client, messageStore *MessageStore, port int) {
	// Handler for sending messages
	http.HandleFunc("/api/send", requireAPIKey(func(w http.ResponseWriter, r *http.Request) {
		// Only allow POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Parse the request body
		var req SendMessageRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		// Validate request
		if req.Recipient == "" {
			http.Error(w, "Recipient is required", http.StatusBadRequest)
			return
		}

		if req.Message == "" && req.MediaPath == "" {
			http.Error(w, "Message or media path is required", http.StatusBadRequest)
			return
		}

		fmt.Println("Received request to send message", req.Message, req.MediaPath)

		// Send the message
		success, message := sendWhatsAppMessage(client, req.Recipient, req.Message, req.MediaPath)
		fmt.Println("Message sent", success, message)
		// Set response headers
		w.Header().Set("Content-Type", "application/json")

		// Set appropriate status code
		if !success {
			w.WriteHeader(http.StatusInternalServerError)
		}

		// Send response
		json.NewEncoder(w).Encode(SendMessageResponse{
			Success: success,
			Message: message,
		})
	}))

	// Handler for backfilling all joined groups into messages.db
	http.HandleFunc("/api/backfill-groups", requireAPIKey(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		groups, err := client.GetJoinedGroups(context.Background())
		if err != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": fmt.Sprintf("Failed to fetch groups: %v", err),
			})
			return
		}

		stored := 0
		for _, g := range groups {
			jid := g.JID.String()
			name := g.Name
			if name == "" {
				name = g.Topic
			}
			if name == "" {
				name = fmt.Sprintf("Group %s", g.JID.User)
			}
			err := messageStore.StoreChat(jid, name, time.Now())
			if err != nil {
				fmt.Printf("Failed to store group %s: %v\n", jid, err)
			} else {
				stored++
			}
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"success":      true,
			"total_groups":  len(groups),
			"stored_groups": stored,
		})
	}))

	// Handler for backfilling all contacts from WhatsApp's synced address book
	http.HandleFunc("/api/backfill-contacts", requireAPIKey(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		contacts, err := client.Store.Contacts.GetAllContacts(context.Background())
		if err != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": fmt.Sprintf("Failed to fetch contacts: %v", err),
			})
			return
		}

		stored := 0
		for jid, contact := range contacts {
			phone := jid.User
			_, err := messageStore.db.Exec(
				`INSERT OR REPLACE INTO contacts (jid, phone, full_name, push_name, business_name, updated_at)
				 VALUES (?, ?, ?, ?, ?, ?)`,
				jid.String(), phone, contact.FullName, contact.PushName, contact.BusinessName, time.Now(),
			)
			if err != nil {
				fmt.Printf("Failed to store contact %s: %v\n", jid.String(), err)
			} else {
				stored++
			}

			// Also ensure they exist in the chats table for search_contacts compatibility
			name := contact.FullName
			if name == "" {
				name = contact.PushName
			}
			if name == "" {
				name = contact.BusinessName
			}
			if name != "" {
				messageStore.db.Exec(
					`INSERT OR IGNORE INTO chats (jid, name, last_message_time) VALUES (?, ?, ?)`,
					jid.String(), name, time.Time{},
				)
			}
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"success":        true,
			"total_contacts":  len(contacts),
			"stored_contacts": stored,
		})
	}))

	// Handler for backfilling group participants for all joined groups
	http.HandleFunc("/api/backfill-group-participants", requireAPIKey(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		groups, err := client.GetJoinedGroups(context.Background())
		if err != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": fmt.Sprintf("Failed to fetch groups: %v", err),
			})
			return
		}

		totalMembers := 0
		groupsProcessed := 0
		for _, g := range groups {
			groupJID := g.JID.String()
			for _, p := range g.Participants {
				memberJID := p.JID.String()
				displayName := ""
				contact, err := client.Store.Contacts.GetContact(context.Background(), p.JID)
				if err == nil && contact.FullName != "" {
					displayName = contact.FullName
				} else if err == nil && contact.PushName != "" {
					displayName = contact.PushName
				}

				_, err = messageStore.db.Exec(
					`INSERT OR REPLACE INTO group_members (group_jid, member_jid, display_name, is_admin, is_super_admin)
					 VALUES (?, ?, ?, ?, ?)`,
					groupJID, memberJID, displayName, p.IsAdmin, p.IsSuperAdmin,
				)
				if err != nil {
					fmt.Printf("Failed to store member %s in %s: %v\n", memberJID, groupJID, err)
				} else {
					totalMembers++
				}
			}
			groupsProcessed++
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"success":          true,
			"groups_processed":  groupsProcessed,
			"total_members":     totalMembers,
		})
	}))

	// Handler for getting members of a specific group
	http.HandleFunc("/api/group-members", requireAPIKey(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		groupJID := r.URL.Query().Get("group_jid")
		if groupJID == "" {
			http.Error(w, `{"error":"group_jid parameter required"}`, http.StatusBadRequest)
			return
		}

		rows, err := messageStore.db.Query(
			`SELECT member_jid, display_name, is_admin, is_super_admin FROM group_members WHERE group_jid = ? ORDER BY display_name`,
			groupJID,
		)
		if err != nil {
			w.Header().Set("Content-Type", "application/json")
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": err.Error()})
			return
		}
		defer rows.Close()

		var members []map[string]interface{}
		for rows.Next() {
			var memberJID, displayName string
			var isAdmin, isSuperAdmin bool
			rows.Scan(&memberJID, &displayName, &isAdmin, &isSuperAdmin)
			members = append(members, map[string]interface{}{
				"jid":            memberJID,
				"display_name":   displayName,
				"is_admin":       isAdmin,
				"is_super_admin": isSuperAdmin,
			})
		}

		w.Header().Set("Content-Type", "application/json")
		json.NewEncoder(w).Encode(map[string]interface{}{
			"success": true,
			"group_jid": groupJID,
			"members": members,
			"count":   len(members),
		})
	}))

	// Handler for requesting WhatsApp history sync for specific chats or all chats with messages
	http.HandleFunc("/api/request-history-sync", requireAPIKey(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		w.Header().Set("Content-Type", "application/json")

		if !client.IsConnected() {
			w.WriteHeader(http.StatusServiceUnavailable)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": "Not connected to WhatsApp",
			})
			return
		}

		// Two-phase sync:
		// 1) FULL_HISTORY_SYNC_ON_DEMAND — asks the phone to re-push the complete
		//    initial history sync (all chats, including those with no local messages).
		// 2) Per-chat HISTORY_SYNC_ON_DEMAND — for chats that already have messages,
		//    request older messages using the oldest known message as anchor.

		requestID := fmt.Sprintf("full-sync-%d", time.Now().UnixMilli())
		fullSyncDays := uint32(365 * 3)
		storageMb := uint32(8192)
		recentDays := uint32(365)
		trueVal := true

		fullSyncMsg := &waE2E.Message{
			ProtocolMessage: &waE2E.ProtocolMessage{
				Type: waE2E.ProtocolMessage_PEER_DATA_OPERATION_REQUEST_MESSAGE.Enum(),
				PeerDataOperationRequestMessage: &waE2E.PeerDataOperationRequestMessage{
					PeerDataOperationRequestType: waE2E.PeerDataOperationRequestType_FULL_HISTORY_SYNC_ON_DEMAND.Enum(),
					FullHistorySyncOnDemandRequest: &waE2E.PeerDataOperationRequestMessage_FullHistorySyncOnDemandRequest{
						RequestMetadata: &waE2E.FullHistorySyncOnDemandRequestMetadata{
							RequestID: &requestID,
						},
						HistorySyncConfig: &waCompanionReg.DeviceProps_HistorySyncConfig{
							FullSyncDaysLimit:    &fullSyncDays,
							FullSyncSizeMbLimit:  &storageMb,
							StorageQuotaMb:       &storageMb,
							RecentSyncDaysLimit:  &recentDays,
							SupportCallLogHistory: &trueVal,
							SupportGroupHistory:   &trueVal,
							OnDemandReady:         &trueVal,
							CompleteOnDemandReady:  &trueVal,
						},
					},
				},
			},
		}

		_, err := client.SendMessage(context.Background(), client.Store.ID.ToNonAD(), fullSyncMsg)
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": fmt.Sprintf("Failed to request full history sync: %v", err),
			})
			return
		}

		fmt.Printf("Full history sync requested (id=%s)\n", requestID)

		// Phase 2: per-chat on-demand sync for chats that already have messages
		type chatSync struct {
			chatJID   string
			msgID     string
			isFromMe  bool
			timestamp time.Time
		}

		rows, err := messageStore.db.Query(
			`SELECT chat_jid, id, is_from_me, timestamp FROM messages
			 WHERE (chat_jid, timestamp) IN (
				SELECT chat_jid, MIN(timestamp) FROM messages GROUP BY chat_jid
			 ) LIMIT 200`,
		)

		onDemandRequested := 0
		onDemandErrors := 0
		if err == nil {
			defer rows.Close()
			for rows.Next() {
				var cs chatSync
				rows.Scan(&cs.chatJID, &cs.msgID, &cs.isFromMe, &cs.timestamp)

				jid, err := types.ParseJID(cs.chatJID)
				if err != nil {
					onDemandErrors++
					continue
				}

				msgInfo := &types.MessageInfo{
					MessageSource: types.MessageSource{
						Chat:     jid,
						IsFromMe: cs.isFromMe,
					},
					ID:        cs.msgID,
					Timestamp: cs.timestamp,
				}

				historyMsg := client.BuildHistorySyncRequest(msgInfo, 100)
				if historyMsg == nil {
					onDemandErrors++
					continue
				}

				_, err = client.SendMessage(context.Background(), client.Store.ID.ToNonAD(), historyMsg)
				if err != nil {
					onDemandErrors++
				} else {
					onDemandRequested++
				}

				time.Sleep(100 * time.Millisecond)
			}
		}

		fmt.Printf("On-demand history requested for %d chats (%d errors)\n", onDemandRequested, onDemandErrors)

		json.NewEncoder(w).Encode(map[string]interface{}{
			"success":             true,
			"message":             "Full history sync + per-chat on-demand sync requested. Messages will arrive over the next few minutes.",
			"full_sync_requested": true,
			"on_demand_chats":     onDemandRequested,
			"on_demand_errors":    onDemandErrors,
		})
	}))

	// Handler for migrating LID-format JIDs to phone number JIDs using whatsmeow's LID map.
	http.HandleFunc("/api/migrate-lid", requireAPIKey(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")

		lidRows, err := messageStore.db.Query(
			`SELECT DISTINCT chat_jid FROM messages WHERE chat_jid LIKE '%@lid'
			 UNION
			 SELECT DISTINCT jid FROM chats WHERE jid LIKE '%@lid'`)
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": err.Error()})
			return
		}
		var lidJIDs []string
		for lidRows.Next() {
			var j string
			lidRows.Scan(&j)
			lidJIDs = append(lidJIDs, j)
		}
		lidRows.Close()

		noopLogger := waLog.Noop
		migrated := 0
		skipped := 0
		for _, lidJIDStr := range lidJIDs {
			jid, err := types.ParseJID(lidJIDStr)
			if err != nil {
				skipped++
				continue
			}
			_, phoneJIDStr := resolveLID(client, jid, noopLogger)
			if phoneJIDStr == lidJIDStr {
				skipped++
				continue
			}

			// Merge messages: update chat_jid from LID to phone number
			_, err = messageStore.db.Exec(
				`UPDATE OR IGNORE messages SET chat_jid = ? WHERE chat_jid = ?`,
				phoneJIDStr, lidJIDStr)
			if err != nil {
				fmt.Printf("Failed to migrate messages %s -> %s: %v\n", lidJIDStr, phoneJIDStr, err)
				skipped++
				continue
			}
			// Clean up any remaining (duplicates that were ignored)
			messageStore.db.Exec(`DELETE FROM messages WHERE chat_jid = ?`, lidJIDStr)

			// Migrate senders within messages too
			lidUser := jid.User
			pnJID, _ := types.ParseJID(phoneJIDStr)
			if pnJID.User != "" && pnJID.User != lidUser {
				messageStore.db.Exec(
					`UPDATE messages SET sender = ? WHERE sender = ?`,
					pnJID.User, lidUser)
			}

			// Merge chat entry: update or delete the LID chat
			existing := messageStore.db.QueryRow(
				`SELECT jid FROM chats WHERE jid = ?`, phoneJIDStr)
			var existingJID string
			if existing.Scan(&existingJID) == nil {
				messageStore.db.Exec(`DELETE FROM chats WHERE jid = ?`, lidJIDStr)
			} else {
				messageStore.db.Exec(
					`UPDATE chats SET jid = ? WHERE jid = ?`,
					phoneJIDStr, lidJIDStr)
			}

			migrated++
			fmt.Printf("Migrated LID %s -> %s\n", lidJIDStr, phoneJIDStr)
		}

		json.NewEncoder(w).Encode(map[string]interface{}{
			"success":  true,
			"migrated": migrated,
			"skipped":  skipped,
			"total":    len(lidJIDs),
		})
	}))

	// Pair via phone number: returns an 8-digit code the user enters on their phone
	// (WhatsApp > Linked Devices > Link a Device > Link with phone number).
	http.HandleFunc("/api/pair-phone", requireAPIKey(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")

		if client.Store.ID != nil {
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": "Already paired. Use /api/re-pair first to unpair, then try again.",
			})
			return
		}

		var body struct {
			Phone string `json:"phone"`
		}
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.Phone == "" {
			w.WriteHeader(http.StatusBadRequest)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": "Provide {\"phone\": \"+1234567890\"} with country code, digits only after +.",
			})
			return
		}

		phone := strings.TrimPrefix(body.Phone, "+")

		if !client.IsConnected() {
			err := client.Connect()
			if err != nil {
				w.WriteHeader(http.StatusInternalServerError)
				json.NewEncoder(w).Encode(map[string]interface{}{
					"success": false,
					"message": fmt.Sprintf("Failed to connect: %v", err),
				})
				return
			}
			time.Sleep(1 * time.Second)
		}

		code, err := client.PairPhone(r.Context(), phone, true, whatsmeow.PairClientChrome, "Chrome (Linux)")
		if err != nil {
			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": false,
				"message": fmt.Sprintf("PairPhone failed: %v", err),
			})
			return
		}

		json.NewEncoder(w).Encode(map[string]interface{}{
			"success": true,
			"code":    code,
			"message": fmt.Sprintf("Enter this code on your phone: %s (valid ~60 seconds). Go to WhatsApp > Linked Devices > Link a Device > Link with phone number.", code),
		})
	}))

	// Handler for re-pairing the WhatsApp session to trigger a full initial history sync.
	// Logs out the current session and exits the process. Supervisor restarts the bridge,
	// which then shows a QR code in the logs for the user to scan.
	http.HandleFunc("/api/re-pair", requireAPIKey(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		w.Header().Set("Content-Type", "application/json")

		fmt.Println("=== RE-PAIR: Logging out current session ===")

		if client.IsConnected() {
			err := client.Logout(context.Background())
			if err != nil {
				fmt.Printf("Logout error (proceeding anyway): %v\n", err)
			}
		}

		json.NewEncoder(w).Encode(map[string]interface{}{
			"success": true,
			"message": "Session logged out. Bridge will restart and show a QR code. Run 'flyctl logs -a whatsapp-mcp-bridge' and scan the QR code with your phone.",
		})

		// Give the response time to flush, then exit so supervisor restarts us
		go func() {
			time.Sleep(1 * time.Second)
			fmt.Println("Exiting for re-pair — supervisor will restart")
			os.Exit(0)
		}()
	}))

	// Handler for downloading media
	http.HandleFunc("/api/download", requireAPIKey(func(w http.ResponseWriter, r *http.Request) {
		// Only allow POST requests
		if r.Method != http.MethodPost {
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
			return
		}

		// Parse the request body
		var req DownloadMediaRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, "Invalid request format", http.StatusBadRequest)
			return
		}

		// Validate request
		if req.MessageID == "" || req.ChatJID == "" {
			http.Error(w, "Message ID and Chat JID are required", http.StatusBadRequest)
			return
		}

		// Download the media
		success, mediaType, filename, path, err := downloadMedia(client, messageStore, req.MessageID, req.ChatJID)

		// Set response headers
		w.Header().Set("Content-Type", "application/json")

		// Handle download result
		if !success || err != nil {
			errMsg := "Unknown error"
			if err != nil {
				errMsg = err.Error()
			}

			w.WriteHeader(http.StatusInternalServerError)
			json.NewEncoder(w).Encode(DownloadMediaResponse{
				Success: false,
				Message: fmt.Sprintf("Failed to download media: %s", errMsg),
			})
			return
		}

		// Send successful response
		json.NewEncoder(w).Encode(DownloadMediaResponse{
			Success:  true,
			Message:  fmt.Sprintf("Successfully downloaded %s media", mediaType),
			Filename: filename,
			Path:     path,
		})
	}))

	// --- Access Key Management Endpoints ---

	http.HandleFunc("/api/access-keys", requireAPIKey(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")

		switch r.Method {
		case http.MethodGet:
			rows, err := messageStore.db.Query(
				`SELECT id, label, scope_all, created_at FROM access_keys ORDER BY created_at`)
			if err != nil {
				w.WriteHeader(http.StatusInternalServerError)
				json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": err.Error()})
				return
			}
			defer rows.Close()
			var keys []map[string]interface{}
			for rows.Next() {
				var id, label string
				var scopeAll bool
				var createdAt time.Time
				rows.Scan(&id, &label, &scopeAll, &createdAt)
				keys = append(keys, map[string]interface{}{
					"id": id, "label": label, "scope_all": scopeAll, "created_at": createdAt,
				})
			}
			if keys == nil {
				keys = []map[string]interface{}{}
			}
			json.NewEncoder(w).Encode(map[string]interface{}{"success": true, "keys": keys})

		case http.MethodPost:
			var body struct {
				ID       string `json:"id"`
				Label    string `json:"label"`
				ScopeAll bool   `json:"scope_all"`
				APIKey   string `json:"api_key"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
				w.WriteHeader(http.StatusBadRequest)
				json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": "Invalid JSON"})
				return
			}
			if body.ID == "" {
				w.WriteHeader(http.StatusBadRequest)
				json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": "id is required"})
				return
			}
			if body.APIKey == "" {
				body.APIKey = generateAPIKey()
			}
			_, err := messageStore.db.Exec(
				`INSERT INTO access_keys (id, api_key, label, scope_all) VALUES (?, ?, ?, ?)`,
				body.ID, body.APIKey, body.Label, body.ScopeAll,
			)
			if err != nil {
				w.WriteHeader(http.StatusConflict)
				json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": err.Error()})
				return
			}
			json.NewEncoder(w).Encode(map[string]interface{}{
				"success": true, "id": body.ID, "api_key": body.APIKey, "scope_all": body.ScopeAll,
			})

		case http.MethodDelete:
			keyID := r.URL.Query().Get("id")
			if keyID == "" {
				w.WriteHeader(http.StatusBadRequest)
				json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": "id query param required"})
				return
			}
			if keyID == "g0d" {
				w.WriteHeader(http.StatusForbidden)
				json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": "cannot delete g0d key"})
				return
			}
			messageStore.db.Exec(`DELETE FROM access_key_scopes WHERE key_id = ?`, keyID)
			messageStore.db.Exec(`DELETE FROM access_keys WHERE id = ?`, keyID)
			json.NewEncoder(w).Encode(map[string]interface{}{"success": true})

		default:
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		}
	}))

	http.HandleFunc("/api/access-key-scopes", requireAPIKey(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")

		switch r.Method {
		case http.MethodGet:
			keyID := r.URL.Query().Get("key_id")
			if keyID == "" {
				w.WriteHeader(http.StatusBadRequest)
				json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": "key_id required"})
				return
			}
			rows, err := messageStore.db.Query(
				`SELECT aks.chat_jid, COALESCE(c.name,'') FROM access_key_scopes aks
				 LEFT JOIN chats c ON aks.chat_jid = c.jid WHERE aks.key_id = ? ORDER BY c.name`, keyID)
			if err != nil {
				w.WriteHeader(http.StatusInternalServerError)
				json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": err.Error()})
				return
			}
			defer rows.Close()
			var scopes []map[string]string
			for rows.Next() {
				var jid, name string
				rows.Scan(&jid, &name)
				scopes = append(scopes, map[string]string{"chat_jid": jid, "name": name})
			}
			if scopes == nil {
				scopes = []map[string]string{}
			}
			json.NewEncoder(w).Encode(map[string]interface{}{"success": true, "key_id": keyID, "scopes": scopes})

		case http.MethodPost:
			var body struct {
				KeyID   string   `json:"key_id"`
				ChatJIDs []string `json:"chat_jids"`
			}
			if err := json.NewDecoder(r.Body).Decode(&body); err != nil || body.KeyID == "" || len(body.ChatJIDs) == 0 {
				w.WriteHeader(http.StatusBadRequest)
				json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": "key_id and chat_jids[] required"})
				return
			}
			added := 0
			for _, jid := range body.ChatJIDs {
				_, err := messageStore.db.Exec(
					`INSERT OR IGNORE INTO access_key_scopes (key_id, chat_jid) VALUES (?, ?)`, body.KeyID, jid)
				if err == nil {
					added++
				}
			}
			json.NewEncoder(w).Encode(map[string]interface{}{"success": true, "added": added})

		case http.MethodDelete:
			keyID := r.URL.Query().Get("key_id")
			chatJID := r.URL.Query().Get("chat_jid")
			if keyID == "" {
				w.WriteHeader(http.StatusBadRequest)
				json.NewEncoder(w).Encode(map[string]interface{}{"success": false, "message": "key_id required"})
				return
			}
			if chatJID != "" {
				messageStore.db.Exec(`DELETE FROM access_key_scopes WHERE key_id = ? AND chat_jid = ?`, keyID, chatJID)
			} else {
				messageStore.db.Exec(`DELETE FROM access_key_scopes WHERE key_id = ?`, keyID)
			}
			json.NewEncoder(w).Encode(map[string]interface{}{"success": true})

		default:
			http.Error(w, "Method not allowed", http.StatusMethodNotAllowed)
		}
	}))

	// Start the server
	serverAddr := fmt.Sprintf(":%d", port)
	fmt.Printf("Starting REST API server on %s...\n", serverAddr)

	// Run server in a goroutine so it doesn't block
	go func() {
		if err := http.ListenAndServe(serverAddr, nil); err != nil {
			fmt.Printf("REST API server error: %v\n", err)
		}
	}()
}

func main() {
	// Set up logger
	logger := waLog.Stdout("Client", "INFO", true)
	logger.Infof("Starting WhatsApp client...")

	// Create database connection for storing session data
	dbLog := waLog.Stdout("Database", "INFO", true)

	// Create directory for database if it doesn't exist
	if err := os.MkdirAll("store", 0755); err != nil {
		logger.Errorf("Failed to create store directory: %v", err)
		return
	}

	container, err := sqlstore.New(context.Background(), "sqlite3", "file:store/whatsapp.db?_foreign_keys=on", dbLog)
	if err != nil {
		logger.Errorf("Failed to connect to database: %v", err)
		return
	}

	// Get device store - This contains session information
	deviceStore, err := container.GetFirstDevice(context.Background())
	if err != nil {
		if err == sql.ErrNoRows {
			// No device exists, create one
			deviceStore = container.NewDevice()
			logger.Infof("Created new device")
		} else {
			logger.Errorf("Failed to get device: %v", err)
			return
		}
	}

	// Configure device properties for full history sync before creating the client
	store.DeviceProps.RequireFullSync = proto.Bool(true)
	store.DeviceProps.HistorySyncConfig = &waCompanionReg.DeviceProps_HistorySyncConfig{
		FullSyncDaysLimit:         proto.Uint32(365 * 3),
		FullSyncSizeMbLimit:       proto.Uint32(8192),
		StorageQuotaMb:            proto.Uint32(8192),
		InlineInitialPayloadInE2EeMsg: proto.Bool(true),
		RecentSyncDaysLimit:       proto.Uint32(365),
		SupportCallLogHistory:     proto.Bool(true),
		SupportBotUserAgentChatHistory: proto.Bool(true),
		SupportGroupHistory:       proto.Bool(true),
		OnDemandReady:             proto.Bool(true),
		CompleteOnDemandReady:     proto.Bool(true),
	}

	// Create client instance
	client := whatsmeow.NewClient(deviceStore, logger)
	if client == nil {
		logger.Errorf("Failed to create WhatsApp client")
		return
	}

	// Initialize message store
	messageStore, err := NewMessageStore()
	if err != nil {
		logger.Errorf("Failed to initialize message store: %v", err)
		return
	}
	defer messageStore.Close()

	// Setup event handling for messages and history sync
	client.AddEventHandler(func(evt interface{}) {
		switch v := evt.(type) {
		case *events.Message:
			// Process regular messages
			handleMessage(client, messageStore, v, logger)

		case *events.HistorySync:
			// Process history sync events
			handleHistorySync(client, messageStore, v, logger)

		case *events.Connected:
			logger.Infof("Connected to WhatsApp")

		case *events.LoggedOut:
			logger.Warnf("Device logged out, please scan QR code to log in again")
		}
	})

	// Start REST API server early so /api/pair-phone is reachable before pairing
	bridgePort := 7481
	if p := os.Getenv("PORT"); p != "" {
		if parsed, err := strconv.Atoi(p); err == nil {
			bridgePort = parsed
		}
	}
	startRESTServer(client, messageStore, bridgePort)
	fmt.Printf("REST server listening on port %d\n", bridgePort)

	// Connect to WhatsApp
	if client.Store.ID == nil {
		// No ID stored — new client. Start QR channel but also allow /api/pair-phone.
		qrChan, _ := client.GetQRChannel(context.Background())
		err = client.Connect()
		if err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}

		paired := make(chan struct{}, 1)
		go func() {
			for evt := range qrChan {
				if evt.Event == "code" {
					fmt.Println("\nScan this QR code or use /api/pair-phone:")
					qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)
				} else if evt.Event == "success" {
					paired <- struct{}{}
					return
				}
			}
		}()

		// Also listen for PairSuccess via event handler
		client.AddEventHandler(func(evt interface{}) {
			if _, ok := evt.(*events.PairSuccess); ok {
				select {
				case paired <- struct{}{}:
				default:
				}
			}
		})

		select {
		case <-paired:
			fmt.Println("\nSuccessfully connected and authenticated!")
		case <-time.After(10 * time.Minute):
			logger.Errorf("Timeout waiting for pairing (QR or phone code)")
			return
		}
	} else {
		err = client.Connect()
		if err != nil {
			logger.Errorf("Failed to connect: %v", err)
			return
		}
	}

	time.Sleep(2 * time.Second)

	if !client.IsConnected() {
		logger.Errorf("Failed to establish stable connection")
		return
	}

	fmt.Println("\n✓ Connected to WhatsApp!")

	exitChan := make(chan os.Signal, 1)
	signal.Notify(exitChan, syscall.SIGINT, syscall.SIGTERM)
	fmt.Println("REST server is running. Press Ctrl+C to disconnect and exit.")
	<-exitChan

	fmt.Println("Disconnecting...")
	client.Disconnect()
}

// GetChatName determines the appropriate name for a chat based on JID and other info
func GetChatName(client *whatsmeow.Client, messageStore *MessageStore, jid types.JID, chatJID string, conversation interface{}, sender string, logger waLog.Logger) string {
	// First, check if chat already exists in database with a name
	var existingName string
	err := messageStore.db.QueryRow("SELECT name FROM chats WHERE jid = ?", chatJID).Scan(&existingName)
	if err == nil && existingName != "" {
		// Chat exists with a name, use that
		logger.Infof("Using existing chat name for %s: %s", chatJID, existingName)
		return existingName
	}

	// Need to determine chat name
	var name string

	if jid.Server == "g.us" {
		// This is a group chat
		logger.Infof("Getting name for group: %s", chatJID)

		// Use conversation data if provided (from history sync)
		if conversation != nil {
			// Extract name from conversation if available
			// This uses type assertions to handle different possible types
			var displayName, convName *string
			// Try to extract the fields we care about regardless of the exact type
			v := reflect.ValueOf(conversation)
			if v.Kind() == reflect.Ptr && !v.IsNil() {
				v = v.Elem()

				// Try to find DisplayName field
				if displayNameField := v.FieldByName("DisplayName"); displayNameField.IsValid() && displayNameField.Kind() == reflect.Ptr && !displayNameField.IsNil() {
					dn := displayNameField.Elem().String()
					displayName = &dn
				}

				// Try to find Name field
				if nameField := v.FieldByName("Name"); nameField.IsValid() && nameField.Kind() == reflect.Ptr && !nameField.IsNil() {
					n := nameField.Elem().String()
					convName = &n
				}
			}

			// Use the name we found
			if displayName != nil && *displayName != "" {
				name = *displayName
			} else if convName != nil && *convName != "" {
				name = *convName
			}
		}

		// If we didn't get a name, try group info
		if name == "" {
			groupInfo, err := client.GetGroupInfo(context.Background(), jid)
			if err == nil && groupInfo.Name != "" {
				name = groupInfo.Name
			} else {
				// Fallback name for groups
				name = fmt.Sprintf("Group %s", jid.User)
			}
		}

		logger.Infof("Using group name: %s", name)
	} else {
		// This is an individual contact
		logger.Infof("Getting name for contact: %s", chatJID)

		// Just use contact info (full name)
		contact, err := client.Store.Contacts.GetContact(context.Background(), jid)
		if err == nil && contact.FullName != "" {
			name = contact.FullName
		} else if sender != "" {
			// Fallback to sender
			name = sender
		} else {
			// Last fallback to JID
			name = jid.User
		}

		logger.Infof("Using contact name: %s", name)
	}

	return name
}

// Handle history sync events
func handleHistorySync(client *whatsmeow.Client, messageStore *MessageStore, historySync *events.HistorySync, logger waLog.Logger) {
	fmt.Printf("Received history sync event with %d conversations\n", len(historySync.Data.Conversations))

	syncedCount := 0
	for _, conversation := range historySync.Data.Conversations {
		// Parse JID from the conversation
		if conversation.ID == nil {
			continue
		}

		rawJID := *conversation.ID

		// Try to parse the JID
		jid, err := types.ParseJID(rawJID)
		if err != nil {
			logger.Warnf("Failed to parse JID %s: %v", rawJID, err)
			continue
		}

		// Resolve LID to phone number JID if available
		jid, chatJID := resolveLID(client, jid, logger)

		// Get appropriate chat name by passing the history sync conversation directly
		name := GetChatName(client, messageStore, jid, chatJID, conversation, "", logger)

		// Process messages
		messages := conversation.Messages
		if len(messages) > 0 {
			// Update chat with latest message timestamp
			latestMsg := messages[0]
			if latestMsg == nil || latestMsg.Message == nil {
				continue
			}

			// Get timestamp from message info
			timestamp := time.Time{}
			if ts := latestMsg.Message.GetMessageTimestamp(); ts != 0 {
				timestamp = time.Unix(int64(ts), 0)
			} else {
				continue
			}

			messageStore.StoreChat(chatJID, name, timestamp)

			// Store messages
			for _, msg := range messages {
				if msg == nil || msg.Message == nil {
					continue
				}

				// Extract text content
				var content string
				if msg.Message.Message != nil {
					if conv := msg.Message.Message.GetConversation(); conv != "" {
						content = conv
					} else if ext := msg.Message.Message.GetExtendedTextMessage(); ext != nil {
						content = ext.GetText()
					}
				}

				// Extract media info
				var mediaType, filename, url string
				var mediaKey, fileSHA256, fileEncSHA256 []byte
				var fileLength uint64

				if msg.Message.Message != nil {
					mediaType, filename, url, mediaKey, fileSHA256, fileEncSHA256, fileLength = extractMediaInfo(msg.Message.Message)
				}

				// Log the message content for debugging
				logger.Infof("Message content: %v, Media Type: %v", content, mediaType)

				// Skip messages with no content and no media
				if content == "" && mediaType == "" {
					continue
				}

				// Determine sender and resolve LID if needed
				var sender string
				isFromMe := false
				if msg.Message.Key != nil {
					if msg.Message.Key.FromMe != nil {
						isFromMe = *msg.Message.Key.FromMe
					}
					if !isFromMe && msg.Message.Key.Participant != nil && *msg.Message.Key.Participant != "" {
						sender = *msg.Message.Key.Participant
						if strings.Contains(sender, "@lid") || strings.HasSuffix(sender, "@lid") {
							parts := strings.SplitN(sender, "@", 2)
							sender = resolveLIDUser(client, parts[0], "lid", logger)
						}
					} else if isFromMe {
						sender = client.Store.ID.User
					} else {
						sender = jid.User
					}
				} else {
					sender = jid.User
				}

				// Store message
				msgID := ""
				if msg.Message.Key != nil && msg.Message.Key.ID != nil {
					msgID = *msg.Message.Key.ID
				}

				// Get message timestamp
				timestamp := time.Time{}
				if ts := msg.Message.GetMessageTimestamp(); ts != 0 {
					timestamp = time.Unix(int64(ts), 0)
				} else {
					continue
				}

				err = messageStore.StoreMessage(
					msgID,
					chatJID,
					sender,
					content,
					timestamp,
					isFromMe,
					mediaType,
					filename,
					url,
					mediaKey,
					fileSHA256,
					fileEncSHA256,
					fileLength,
				)
				if err != nil {
					logger.Warnf("Failed to store history message: %v", err)
				} else {
					syncedCount++
					// Log successful message storage
					if mediaType != "" {
						logger.Infof("Stored message: [%s] %s -> %s: [%s: %s] %s",
							timestamp.Format("2006-01-02 15:04:05"), sender, chatJID, mediaType, filename, content)
					} else {
						logger.Infof("Stored message: [%s] %s -> %s: %s",
							timestamp.Format("2006-01-02 15:04:05"), sender, chatJID, content)
					}
				}
			}
		}
	}

	fmt.Printf("History sync complete. Stored %d messages.\n", syncedCount)
}

// requestHistorySync is now handled via the /api/request-history-sync endpoint.

// analyzeOggOpus tries to extract duration and generate a simple waveform from an Ogg Opus file
func analyzeOggOpus(data []byte) (duration uint32, waveform []byte, err error) {
	// Try to detect if this is a valid Ogg file by checking for the "OggS" signature
	// at the beginning of the file
	if len(data) < 4 || string(data[0:4]) != "OggS" {
		return 0, nil, fmt.Errorf("not a valid Ogg file (missing OggS signature)")
	}

	// Parse Ogg pages to find the last page with a valid granule position
	var lastGranule uint64
	var sampleRate uint32 = 48000 // Default Opus sample rate
	var preSkip uint16 = 0
	var foundOpusHead bool

	// Scan through the file looking for Ogg pages
	for i := 0; i < len(data); {
		// Check if we have enough data to read Ogg page header
		if i+27 >= len(data) {
			break
		}

		// Verify Ogg page signature
		if string(data[i:i+4]) != "OggS" {
			// Skip until next potential page
			i++
			continue
		}

		// Extract header fields
		granulePos := binary.LittleEndian.Uint64(data[i+6 : i+14])
		pageSeqNum := binary.LittleEndian.Uint32(data[i+18 : i+22])
		numSegments := int(data[i+26])

		// Extract segment table
		if i+27+numSegments >= len(data) {
			break
		}
		segmentTable := data[i+27 : i+27+numSegments]

		// Calculate page size
		pageSize := 27 + numSegments
		for _, segLen := range segmentTable {
			pageSize += int(segLen)
		}

		// Check if we're looking at an OpusHead packet (should be in first few pages)
		if !foundOpusHead && pageSeqNum <= 1 {
			// Look for "OpusHead" marker in this page
			pageData := data[i : i+pageSize]
			headPos := bytes.Index(pageData, []byte("OpusHead"))
			if headPos >= 0 && headPos+12 < len(pageData) {
				// Found OpusHead, extract sample rate and pre-skip
				// OpusHead format: Magic(8) + Version(1) + Channels(1) + PreSkip(2) + SampleRate(4) + ...
				headPos += 8 // Skip "OpusHead" marker
				// PreSkip is 2 bytes at offset 10
				if headPos+12 <= len(pageData) {
					preSkip = binary.LittleEndian.Uint16(pageData[headPos+10 : headPos+12])
					sampleRate = binary.LittleEndian.Uint32(pageData[headPos+12 : headPos+16])
					foundOpusHead = true
					fmt.Printf("Found OpusHead: sampleRate=%d, preSkip=%d\n", sampleRate, preSkip)
				}
			}
		}

		// Keep track of last valid granule position
		if granulePos != 0 {
			lastGranule = granulePos
		}

		// Move to next page
		i += pageSize
	}

	if !foundOpusHead {
		fmt.Println("Warning: OpusHead not found, using default values")
	}

	// Calculate duration based on granule position
	if lastGranule > 0 {
		// Formula for duration: (lastGranule - preSkip) / sampleRate
		durationSeconds := float64(lastGranule-uint64(preSkip)) / float64(sampleRate)
		duration = uint32(math.Ceil(durationSeconds))
		fmt.Printf("Calculated Opus duration from granule: %f seconds (lastGranule=%d)\n",
			durationSeconds, lastGranule)
	} else {
		// Fallback to rough estimation if granule position not found
		fmt.Println("Warning: No valid granule position found, using estimation")
		durationEstimate := float64(len(data)) / 2000.0 // Very rough approximation
		duration = uint32(durationEstimate)
	}

	// Make sure we have a reasonable duration (at least 1 second, at most 300 seconds)
	if duration < 1 {
		duration = 1
	} else if duration > 300 {
		duration = 300
	}

	// Generate waveform
	waveform = placeholderWaveform(duration)

	fmt.Printf("Ogg Opus analysis: size=%d bytes, calculated duration=%d sec, waveform=%d bytes\n",
		len(data), duration, len(waveform))

	return duration, waveform, nil
}

// min returns the smaller of x or y
func min(x, y int) int {
	if x < y {
		return x
	}
	return y
}

// placeholderWaveform generates a synthetic waveform for WhatsApp voice messages
// that appears natural with some variability based on the duration
func placeholderWaveform(duration uint32) []byte {
	// WhatsApp expects a 64-byte waveform for voice messages
	const waveformLength = 64
	waveform := make([]byte, waveformLength)

	// Seed the random number generator for consistent results with the same duration
	rand.Seed(int64(duration))

	// Create a more natural looking waveform with some patterns and variability
	// rather than completely random values

	// Base amplitude and frequency - longer messages get faster frequency
	baseAmplitude := 35.0
	frequencyFactor := float64(min(int(duration), 120)) / 30.0

	for i := range waveform {
		// Position in the waveform (normalized 0-1)
		pos := float64(i) / float64(waveformLength)

		// Create a wave pattern with some randomness
		// Use multiple sine waves of different frequencies for more natural look
		val := baseAmplitude * math.Sin(pos*math.Pi*frequencyFactor*8)
		val += (baseAmplitude / 2) * math.Sin(pos*math.Pi*frequencyFactor*16)

		// Add some randomness to make it look more natural
		val += (rand.Float64() - 0.5) * 15

		// Add some fade-in and fade-out effects
		fadeInOut := math.Sin(pos * math.Pi)
		val = val * (0.7 + 0.3*fadeInOut)

		// Center around 50 (typical voice baseline)
		val = val + 50

		// Ensure values stay within WhatsApp's expected range (0-100)
		if val < 0 {
			val = 0
		} else if val > 100 {
			val = 100
		}

		waveform[i] = byte(val)
	}

	return waveform
}
