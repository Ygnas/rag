package whatsapp

import (
	"bytes"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log"
	"mime/multipart"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	"github.com/mdp/qrterminal/v3"
	"go.mau.fi/whatsmeow"
	"go.mau.fi/whatsmeow/proto/waE2E"
	"go.mau.fi/whatsmeow/store"
	"go.mau.fi/whatsmeow/store/sqlstore"
	"go.mau.fi/whatsmeow/types"
	"go.mau.fi/whatsmeow/types/events"
	waLog "go.mau.fi/whatsmeow/util/log"

	"whatsapp-bot/models"

	_ "github.com/mattn/go-sqlite3"
)

// Client wraps the WhatsApp client with additional functionality
type Client struct {
	client          *whatsmeow.Client
	db              *models.Database
	deviceStore     *store.Device
	eventHandlerID  uint32
	mediaDir        string
	voiceAPIBaseURL string
	httpClient      *http.Client
	convertOggToWav bool // Convert OGG to WAV before sending to voice-api-server (default: true)
}

// NewClient creates a new WhatsApp client
func NewClient(dbPath, mediaDir, voiceAPIBaseURL string) (*Client, error) {
	// Create device store
	ctx := context.Background()
	logger := waLog.Noop
	container, err := sqlstore.New(ctx, "sqlite3", "file:"+dbPath+"?_foreign_keys=on", logger)
	if err != nil {
		return nil, fmt.Errorf("failed to create device store: %w", err)
	}

	deviceStore, err := container.GetFirstDevice(ctx)
	if err != nil {
		return nil, fmt.Errorf("failed to get device: %w", err)
	}

	// Create WhatsApp client
	client := whatsmeow.NewClient(deviceStore, nil)

	// Create database
	database, err := models.NewDatabase(dbPath + "_messages.db")
	if err != nil {
		return nil, fmt.Errorf("failed to create database: %w", err)
	}

	// Create media directory
	if err := os.MkdirAll(mediaDir, 0755); err != nil {
		return nil, fmt.Errorf("failed to create media directory: %w", err)
	}

	// Read conversion flag from environment (default: true, convert OGG to WAV)
	convertOggToWav := true
	if envValue := os.Getenv("DISABLE_OGG_TO_WAV_CONVERSION"); envValue != "" {
		if disabled, err := strconv.ParseBool(envValue); err == nil {
			convertOggToWav = !disabled // Invert because env var is "DISABLE"
		}
	}

	c := &Client{
		client:          client,
		db:              database,
		deviceStore:     deviceStore,
		mediaDir:        mediaDir,
		voiceAPIBaseURL: voiceAPIBaseURL,
		httpClient:      &http.Client{Timeout: 60 * time.Second},
		convertOggToWav: convertOggToWav,
	}

	// Add event handler
	c.eventHandlerID = client.AddEventHandler(c.eventHandler)

	return c, nil
}

// Connect connects to WhatsApp
func (c *Client) Connect(ctx context.Context) error {
	log.Printf("🔌 Attempting to connect to WhatsApp...")

	if c.client.Store.ID == nil {
		// No ID stored, new login
		log.Printf("📱 No stored session found, initiating new login...")
		qrChan, _ := c.client.GetQRChannel(ctx)
		err := c.client.Connect()
		if err != nil {
			log.Printf("❌ Failed to connect: %v", err)
			return fmt.Errorf("failed to connect: %w", err)
		}

		for evt := range qrChan {
			if evt.Event == "code" {
				// Display QR code
				fmt.Println("\n📱 WhatsApp Registration")
				fmt.Println("Scan the QR code below with WhatsApp:")
				qrterminal.GenerateHalfBlock(evt.Code, qrterminal.L, os.Stdout)

				// Display PIN instructions
				fmt.Println("\n🔑 Or use Pairing Code (PIN):")
				fmt.Println("   WhatsApp > Settings > Linked Devices > Link a Device")
				fmt.Println("   > Link with Phone Number Instead")
				fmt.Println("   (The PIN will appear on your phone)")

				// Display PIN if detected in event
				if len(evt.Code) == 8 && isNumeric(evt.Code) {
					fmt.Println("\n🔑 Pairing Code:", evt.Code)
				}
				fmt.Println()
			} else if evt.Event == "timeout" {
				fmt.Println("\n⏱️ QR Code expired. Waiting for new code...")
				fmt.Println("Or use PIN method: WhatsApp > Settings > Linked Devices > Link a Device > Link with Phone Number Instead")
				log.Printf("⚠️ QR code expired, waiting for new code or pairing code...")
			} else if evt.Event == "success" {
				fmt.Println("\n✅ Successfully logged in!")
				log.Printf("✅ WhatsApp login successful")
				break
			} else {
				log.Printf("📱 QR Channel Event: %s", evt.Event)
				// Display PIN if detected
				if evt.Code != "" && len(evt.Code) == 8 && isNumeric(evt.Code) {
					fmt.Println("\n🔑 Pairing Code:", evt.Code)
					fmt.Println("Enter this in WhatsApp: Settings > Linked Devices > Link a Device > Link with Phone Number Instead")
				}
			}
		}
	} else {
		// Already logged in, just connect
		log.Printf("🔄 Using stored session, connecting...")
		err := c.client.Connect()
		if err != nil {
			log.Printf("❌ Failed to connect: %v", err)
			return fmt.Errorf("failed to connect: %w", err)
		}
		log.Printf("✅ WhatsApp connected successfully")
	}

	return nil
}

// Disconnect disconnects from WhatsApp
func (c *Client) Disconnect() {
	c.client.Disconnect()
}

// IsConnected checks if the WhatsApp client is connected
func (c *Client) IsConnected() bool {
	return c.client.IsConnected()
}

// EnsureConnected ensures the client is connected, reconnecting if necessary
func (c *Client) EnsureConnected(ctx context.Context) error {
	if !c.IsConnected() {
		log.Printf("⚠️ WhatsApp client not connected, attempting to reconnect...")
		return c.Connect(ctx)
	}
	return nil
}

// Close closes the client and database
func (c *Client) Close() error {
	c.client.RemoveEventHandler(c.eventHandlerID)
	return c.db.Close()
}

// eventHandler handles WhatsApp events
func (c *Client) eventHandler(evt interface{}) {
	switch v := evt.(type) {
	case *events.Message:
		log.Printf("🔔 Processing message event")
		c.handleMessage(v)
	case *events.Receipt:
		log.Printf("🔔 Processing receipt event")
		c.handleReceipt(v)
	case *events.Presence:
		log.Printf("🔔 Processing presence event")
		c.handlePresence(v)
	default:
		log.Printf("🔔 Processing unknown event type: %T", evt)
	}
}

// handleMessage processes incoming messages and routes them to appropriate handlers
func (c *Client) handleMessage(evt *events.Message) {
	msg := evt.Message
	info := evt.Info

	// Log message received
	log.Printf("📨 Message received from %s in chat %s (ID: %s)",
		info.Sender.String(),
		info.Chat.String(),
		info.ID)

	// Route message to appropriate handler based on type
	if msg.GetConversation() != "" {
		c.handleTextMessage(evt, msg.GetConversation())
	} else if msg.GetExtendedTextMessage() != nil {
		c.handleTextMessage(evt, msg.GetExtendedTextMessage().GetText())
	} else if msg.GetImageMessage() != nil {
		c.handleImageMessage(evt, msg.GetImageMessage())
	} else if msg.GetVideoMessage() != nil {
		c.handleVideoMessage(evt, msg.GetVideoMessage())
	} else if msg.GetAudioMessage() != nil {
		c.handleAudioMessage(evt, msg.GetAudioMessage())
	} else if msg.GetDocumentMessage() != nil {
		c.handleDocumentMessage(evt, msg.GetDocumentMessage())
	} else {
		log.Printf("❓ Unknown message type")
		c.handleUnknownMessage(evt)
	}
}

// handleTextMessage processes text messages
func (c *Client) handleTextMessage(evt *events.Message, content string) {
	info := evt.Info

	log.Printf("💬 Text message: %s", content)

	// Store message in database
	message := &models.Message{
		Time:      info.Timestamp,
		Sender:    info.Sender.String(),
		Content:   content,
		IsFromMe:  info.IsFromMe,
		MediaType: "text",
		Filename:  "",
		ChatJID:   info.Chat.String(),
		MessageID: info.ID,
	}

	if err := c.db.StoreMessage(message); err != nil {
		log.Printf("❌ Failed to store text message: %v", err)
	} else {
		log.Printf("✅ Text message stored successfully")
	}

	// Update chat info
	c.updateChatInfo(info.Chat, content, info.Timestamp)

	// Process text message for commands or auto-replies
	c.processTextMessage(evt, content)
}

// handleAudioMessage processes audio/voice messages
func (c *Client) handleAudioMessage(evt *events.Message, audioMsg *waE2E.AudioMessage) {
	info := evt.Info

	log.Printf("🎵 Audio message received")
	log.Printf("📊 Audio details - Duration: %d seconds, PTT: %v, MIME: %s",
		audioMsg.GetSeconds(), audioMsg.GetPTT(), audioMsg.GetMimetype())

	// Determine if it's a voice message (PTT) or regular audio
	messageType := "audio"
	if audioMsg.GetPTT() {
		messageType = "voice"
		log.Printf("🎤 Voice message (PTT)")
	} else {
		log.Printf("🎵 Regular audio message")
	}

	// Store message in database
	message := &models.Message{
		Time:      info.Timestamp,
		Sender:    info.Sender.String(),
		Content:   fmt.Sprintf("[%s Message]", strings.ToUpper(messageType[:1])+messageType[1:]),
		IsFromMe:  info.IsFromMe,
		MediaType: messageType,
		Filename:  "",
		ChatJID:   info.Chat.String(),
		MessageID: info.ID,
	}

	if err := c.db.StoreMessage(message); err != nil {
		log.Printf("❌ Failed to store audio message: %v", err)
	} else {
		log.Printf("✅ Audio message stored successfully")
	}

	// Update chat info
	c.updateChatInfo(info.Chat, fmt.Sprintf("[%s Message]", strings.ToUpper(messageType[:1])+messageType[1:]), info.Timestamp)

	// Process audio/voice message
	c.processAudioMessage(evt, audioMsg, messageType)
}

// handleImageMessage processes image messages
func (c *Client) handleImageMessage(evt *events.Message, imageMsg *waE2E.ImageMessage) {
	info := evt.Info
	caption := imageMsg.GetCaption()

	log.Printf("🖼️ Image message (caption: %s)", caption)

	// Store message in database
	message := &models.Message{
		Time:      info.Timestamp,
		Sender:    info.Sender.String(),
		Content:   caption,
		IsFromMe:  info.IsFromMe,
		MediaType: "image",
		Filename:  "",
		ChatJID:   info.Chat.String(),
		MessageID: info.ID,
	}

	if err := c.db.StoreMessage(message); err != nil {
		log.Printf("❌ Failed to store image message: %v", err)
	} else {
		log.Printf("✅ Image message stored successfully")
	}

	// Update chat info
	c.updateChatInfo(info.Chat, caption, info.Timestamp)

	// TODO: Add custom image processing logic here
	// e.g., OCR, image analysis, etc.
}

// handleVideoMessage processes video messages
func (c *Client) handleVideoMessage(evt *events.Message, videoMsg *waE2E.VideoMessage) {
	info := evt.Info
	caption := videoMsg.GetCaption()

	log.Printf("🎥 Video message (caption: %s)", caption)

	// Store message in database
	message := &models.Message{
		Time:      info.Timestamp,
		Sender:    info.Sender.String(),
		Content:   caption,
		IsFromMe:  info.IsFromMe,
		MediaType: "video",
		Filename:  "",
		ChatJID:   info.Chat.String(),
		MessageID: info.ID,
	}

	if err := c.db.StoreMessage(message); err != nil {
		log.Printf("❌ Failed to store video message: %v", err)
	} else {
		log.Printf("✅ Video message stored successfully")
	}

	// Update chat info
	c.updateChatInfo(info.Chat, caption, info.Timestamp)

	// TODO: Add custom video processing logic here
	// e.g., video analysis, thumbnail generation, etc.
}

// handleDocumentMessage processes document messages
func (c *Client) handleDocumentMessage(evt *events.Message, docMsg *waE2E.DocumentMessage) {
	info := evt.Info
	filename := docMsg.GetFileName()
	caption := docMsg.GetCaption()

	log.Printf("📄 Document message (filename: %s, caption: %s)", filename, caption)

	// Store message in database
	message := &models.Message{
		Time:      info.Timestamp,
		Sender:    info.Sender.String(),
		Content:   caption,
		IsFromMe:  info.IsFromMe,
		MediaType: "document",
		Filename:  filename,
		ChatJID:   info.Chat.String(),
		MessageID: info.ID,
	}

	if err := c.db.StoreMessage(message); err != nil {
		log.Printf("❌ Failed to store document message: %v", err)
	} else {
		log.Printf("✅ Document message stored successfully")
	}

	// Update chat info
	c.updateChatInfo(info.Chat, caption, info.Timestamp)

	// TODO: Add custom document processing logic here
	// e.g., file type detection, content extraction, etc.
}

// handleUnknownMessage processes unknown message types
func (c *Client) handleUnknownMessage(evt *events.Message) {
	info := evt.Info

	log.Printf("❓ Unknown message type from %s", info.Sender.String())

	// Store as unknown message type
	message := &models.Message{
		Time:      info.Timestamp,
		Sender:    info.Sender.String(),
		Content:   "[Unknown Message Type]",
		IsFromMe:  info.IsFromMe,
		MediaType: "unknown",
		Filename:  "",
		ChatJID:   info.Chat.String(),
		MessageID: info.ID,
	}

	if err := c.db.StoreMessage(message); err != nil {
		log.Printf("❌ Failed to store unknown message: %v", err)
	} else {
		log.Printf("✅ Unknown message stored successfully")
	}

	// Update chat info
	c.updateChatInfo(info.Chat, "[Unknown Message Type]", info.Timestamp)
}

// handleReceipt processes message receipts
func (c *Client) handleReceipt(evt *events.Receipt) {
	log.Printf("📋 Receipt received - Type: %s, MessageIDs: %v",
		evt.Type, evt.MessageIDs)
	// Handle read receipts, delivery receipts, etc.
}

// handlePresence processes presence updates
func (c *Client) handlePresence(evt *events.Presence) {
	log.Printf("👤 Presence update - From: %s, LastSeen: %s",
		evt.From.String(), evt.LastSeen.String())
	// Handle online/offline status updates
}

// updateChatInfo updates chat information in the database
func (c *Client) updateChatInfo(chatJID types.JID, lastMessage string, timestamp time.Time) {
	chat := &models.Chat{
		JID:             chatJID.String(),
		LastMessage:     lastMessage,
		LastMessageTime: timestamp,
		IsGroup:         chatJID.Server == types.GroupServer,
	}

	// Try to get chat name
	if chatJID.Server == types.GroupServer {
		// For groups, we might need to get the group info
		// For now, we'll use the JID as the name
		chat.Name = chatJID.String()
	} else {
		// For individual chats, try to get contact name
		ctx := context.Background()
		contact, err := c.client.Store.Contacts.GetContact(ctx, chatJID)
		if err == nil && contact.FullName != "" {
			chat.Name = contact.FullName
		} else {
			chat.Name = chatJID.String()
		}
	}

	c.db.StoreChat(chat)
}

// SearchContacts searches for contacts by name or phone number
func (c *Client) SearchContacts(query string) ([]*models.Contact, error) {
	// First search in our database
	dbContacts, err := c.db.SearchContacts(query)
	if err != nil {
		return nil, err
	}

	// Also search in WhatsApp client's contact list
	ctx := context.Background()
	allContacts, err := c.client.Store.Contacts.GetAllContacts(ctx)
	if err != nil {
		return dbContacts, nil // Return database results if client search fails
	}

	var clientContacts []*models.Contact
	for jid, contact := range allContacts {
		if strings.Contains(strings.ToLower(contact.FullName), strings.ToLower(query)) ||
			strings.Contains(strings.ToLower(contact.PushName), strings.ToLower(query)) ||
			strings.Contains(jid.String(), query) {

			clientContact := &models.Contact{
				JID:       jid.String(),
				Name:      contact.FullName,
				PushName:  contact.PushName,
				IsGroup:   false,
				IsBlocked: contact.BusinessName != "",
			}
			clientContacts = append(clientContacts, clientContact)
		}
	}

	// Merge and deduplicate results
	contactMap := make(map[string]*models.Contact)
	for _, contact := range dbContacts {
		contactMap[contact.JID] = contact
	}
	for _, contact := range clientContacts {
		if _, exists := contactMap[contact.JID]; !exists {
			contactMap[contact.JID] = contact
		}
	}

	var result []*models.Contact
	for _, contact := range contactMap {
		result = append(result, contact)
	}

	return result, nil
}

// ListMessages retrieves messages with optional filters
func (c *Client) ListMessages(chatJID string, limit, offset int) ([]*models.Message, error) {
	return c.db.GetMessages(chatJID, limit, offset)
}

// ListChats lists available chats with metadata
func (c *Client) ListChats() ([]*models.Chat, error) {
	return c.db.GetChats()
}

// GetChat gets information about a specific chat
func (c *Client) GetChat(chatJID string) (*models.Chat, error) {
	return c.db.GetChatByJID(chatJID)
}

// GetDirectChatByContact finds a direct chat with a specific contact
func (c *Client) GetDirectChatByContact(contactJID string) (*models.Chat, error) {
	// For direct chats, the chat JID is the same as the contact JID
	return c.db.GetChatByJID(contactJID)
}

// GetContactChats lists all chats involving a specific contact
func (c *Client) GetContactChats(contactJID string) ([]*models.Chat, error) {
	return c.db.GetChatsByContact(contactJID)
}

// GetLastInteraction gets the most recent message with a contact
func (c *Client) GetLastInteraction(contactJID string) (*models.Message, error) {
	return c.db.GetLastMessageWithContact(contactJID)
}

// GetMessageContext retrieves context around a specific message
func (c *Client) GetMessageContext(messageID string, contextSize int) ([]*models.Message, error) {
	// Get the target message
	targetMsg, err := c.db.GetMessageByID(messageID)
	if err != nil {
		return nil, err
	}

	// Get messages before and after
	beforeMsgs, err := c.db.GetMessages(targetMsg.ChatJID, contextSize, 0)
	if err != nil {
		return nil, err
	}

	// Filter to get context around the target message
	var context []*models.Message
	for _, msg := range beforeMsgs {
		if msg.MessageID == messageID {
			// Found the target message, add surrounding context
			startIdx := max(0, len(beforeMsgs)-contextSize)
			endIdx := min(len(beforeMsgs), len(beforeMsgs)+contextSize)
			context = beforeMsgs[startIdx:endIdx]
			break
		}
	}

	return context, nil
}

// SendMessage sends a WhatsApp message to a specified phone number or group JID
func (c *Client) SendMessage(recipient string, message string) error {
	// Ensure client is connected before sending
	ctx := context.Background()
	if err := c.EnsureConnected(ctx); err != nil {
		return fmt.Errorf("failed to ensure connection: %w", err)
	}

	log.Printf("📤 Sending message to %s: %s", recipient, message)

	recipientJID, err := types.ParseJID(recipient)
	if err != nil {
		return fmt.Errorf("invalid recipient JID: %w", err)
	}

	msg := &waE2E.Message{
		Conversation: &message,
	}

	resp, err := c.client.SendMessage(ctx, recipientJID, msg)
	if err != nil {
		log.Printf("❌ Failed to send message: %v", err)
		return fmt.Errorf("failed to send message: %w", err)
	}

	// Store the sent message in the database
	sentMessage := &models.Message{
		Time:      time.Now(),
		Sender:    c.client.Store.ID.String(), // Our own JID
		Content:   message,
		IsFromMe:  true,
		MediaType: "text",
		Filename:  "",
		ChatJID:   recipientJID.String(),
		MessageID: resp.ID, // Use the actual message ID from WhatsApp response
	}

	if err := c.db.StoreMessage(sentMessage); err != nil {
		log.Printf("⚠️ Failed to store sent message in database: %v", err)
	} else {
		log.Printf("✅ Sent message stored in database")
	}

	// Update chat info
	c.updateChatInfo(recipientJID, message, time.Now())

	log.Printf("✅ Message sent successfully to %s", recipient)
	return nil
}

// SendFile sends a file to a specified recipient
func (c *Client) SendFile(recipient string, filePath string, caption string) error {
	// Ensure client is connected before sending
	ctx := context.Background()
	if err := c.EnsureConnected(ctx); err != nil {
		return fmt.Errorf("failed to ensure connection: %w", err)
	}

	log.Printf("📤 Sending file to %s: %s (caption: %s)", recipient, filePath, caption)

	recipientJID, err := types.ParseJID(recipient)
	if err != nil {
		return fmt.Errorf("invalid recipient JID: %w", err)
	}

	// Read file
	file, err := os.Open(filePath)
	if err != nil {
		return fmt.Errorf("failed to open file: %w", err)
	}
	defer file.Close()

	// Get file info
	fileInfo, err := file.Stat()
	if err != nil {
		return fmt.Errorf("failed to get file info: %w", err)
	}

	// Read file content - for now we'll skip the actual file upload
	// In a real implementation, you would upload the file data
	_, err = io.ReadAll(file)
	if err != nil {
		return fmt.Errorf("failed to read file: %w", err)
	}

	// Determine media type based on file extension
	ext := strings.ToLower(filepath.Ext(filePath))
	var mediaType string
	var msg *waE2E.Message

	switch ext {
	case ".jpg", ".jpeg", ".png", ".gif", ".webp":
		mediaType = "image"
		fileSizePtr := uint64(fileInfo.Size())
		msg = &waE2E.Message{
			ImageMessage: &waE2E.ImageMessage{
				Caption:    &caption,
				Mimetype:   &mediaType,
				FileLength: &fileSizePtr,
			},
		}
	case ".mp4", ".avi", ".mov", ".mkv":
		mediaType = "video"
		fileSizePtr := uint64(fileInfo.Size())
		msg = &waE2E.Message{
			VideoMessage: &waE2E.VideoMessage{
				Caption:    &caption,
				Mimetype:   &mediaType,
				FileLength: &fileSizePtr,
			},
		}
	case ".ogg", ".opus":
		mediaType = "audio"
		fileSizePtr := uint64(fileInfo.Size())
		msg = &waE2E.Message{
			AudioMessage: &waE2E.AudioMessage{
				Mimetype:   &mediaType,
				FileLength: &fileSizePtr,
			},
		}
	default:
		// Default to document
		mediaType = "application/octet-stream"
		fileName := fileInfo.Name()
		fileSizePtr := uint64(fileInfo.Size())
		msg = &waE2E.Message{
			DocumentMessage: &waE2E.DocumentMessage{
				Caption:    &caption,
				Mimetype:   &mediaType,
				FileName:   &fileName,
				FileLength: &fileSizePtr,
			},
		}
	}

	_, err = c.client.SendMessage(context.Background(), recipientJID, msg)
	return err
}

// SendAudioMessage sends an audio file as a WhatsApp voice message
func (c *Client) SendAudioMessage(recipient string, filePath string) error {
	// Ensure client is connected before sending
	ctx := context.Background()
	if err := c.EnsureConnected(ctx); err != nil {
		return fmt.Errorf("failed to ensure connection: %w", err)
	}

	log.Printf("📤 Sending audio message to %s: %s", recipient, filePath)

	recipientJID, err := types.ParseJID(recipient)
	if err != nil {
		return fmt.Errorf("invalid recipient JID: %w", err)
	}

	// Read file
	file, err := os.Open(filePath)
	if err != nil {
		return fmt.Errorf("failed to open file: %w", err)
	}
	defer file.Close()

	// Get file info
	fileInfo, err := file.Stat()
	if err != nil {
		return fmt.Errorf("failed to get file info: %w", err)
	}

	// Read file content
	fileData, err := io.ReadAll(file)
	if err != nil {
		return fmt.Errorf("failed to read file: %w", err)
	}

	log.Printf("📊 Audio file details - Size: %d bytes, Name: %s", fileInfo.Size(), fileInfo.Name())

	// Determine MIME type based on file extension
	mimeType := getAudioMimeType(filePath)
	log.Printf("🎵 Detected MIME type: %s", mimeType)

	// Get audio duration using ffprobe
	duration, err := getAudioDuration(filePath)
	if err != nil {
		log.Printf("⚠️ Could not determine audio duration: %v", err)
		// Estimate duration (rough estimate: assume 1 second per 16KB for opus)
		estimatedDuration := float64(fileInfo.Size()) / 16000.0
		if estimatedDuration < 1 {
			estimatedDuration = 1
		}
		duration = estimatedDuration
		log.Printf("⏱️ Using estimated duration: %.2f seconds", duration)
	} else {
		log.Printf("⏱️ Audio duration: %.2f seconds", duration)
	}

	// Upload media to WhatsApp servers with retry logic
	var uploaded whatsmeow.UploadResponse
	maxRetries := 3
	for attempt := 1; attempt <= maxRetries; attempt++ {
		log.Printf("🔄 Upload attempt %d/%d", attempt, maxRetries)

		uploaded, err = c.client.Upload(ctx, fileData, whatsmeow.MediaAudio)
		if err == nil {
			log.Printf("✅ Audio file uploaded successfully, URL: %s", uploaded.URL)
			break
		}

		log.Printf("❌ Upload attempt %d failed: %v", attempt, err)
		if attempt < maxRetries {
			log.Printf("⏳ Retrying in 2 seconds...")
			time.Sleep(2 * time.Second)
		}
	}

	if err != nil {
		log.Printf("❌ Failed to upload audio file after %d attempts: %v", maxRetries, err)
		return fmt.Errorf("failed to upload audio file after %d attempts: %w", maxRetries, err)
	}

	// Create audio message
	fileSizePtr := uint64(fileInfo.Size())
	msg := &waE2E.Message{
		AudioMessage: &waE2E.AudioMessage{
			URL:               &uploaded.URL,
			Mimetype:          stringPtr("audio/ogg; codecs=opus"), // Use proper MIME type for voice messages
			FileLength:        &fileSizePtr,
			Seconds:           uint32Ptr(uint32(duration)), // Use actual duration
			PTT:               boolPtr(true),               // Mark as voice message
			FileSHA256:        uploaded.FileSHA256,
			FileEncSHA256:     uploaded.FileEncSHA256,
			MediaKey:          uploaded.MediaKey,
			DirectPath:        &uploaded.DirectPath,        // Add missing DirectPath
			MediaKeyTimestamp: int64Ptr(time.Now().Unix()), // Add missing MediaKeyTimestamp
		},
	}

	resp, err := c.client.SendMessage(ctx, recipientJID, msg)
	if err != nil {
		log.Printf("❌ Failed to send audio message: %v", err)
		return fmt.Errorf("failed to send audio message: %w", err)
	}

	// Store the sent audio message in the database
	audioMessage := &models.Message{
		Time:      time.Now(),
		Sender:    c.client.Store.ID.String(), // Our own JID
		Content:   "[Voice Message]",          // Placeholder content for audio messages
		IsFromMe:  true,
		MediaType: "voice",
		Filename:  filepath.Base(filePath),
		ChatJID:   recipientJID.String(),
		MessageID: resp.ID, // Use the actual message ID from WhatsApp response
	}

	if err := c.db.StoreMessage(audioMessage); err != nil {
		log.Printf("⚠️ Failed to store sent audio message in database: %v", err)
	} else {
		log.Printf("✅ Sent audio message stored in database")
	}

	// Update chat info
	c.updateChatInfo(recipientJID, "[Voice Message]", time.Now())

	log.Printf("✅ Audio message sent successfully to %s", recipient)
	return nil
}

// Helper functions for creating pointers
func stringPtr(s string) *string {
	return &s
}

func uint32Ptr(u uint32) *uint32 {
	return &u
}

func boolPtr(b bool) *bool {
	return &b
}

func int64Ptr(i int64) *int64 {
	return &i
}

// getAudioMimeType determines the MIME type based on file extension
func getAudioMimeType(filePath string) string {
	ext := strings.ToLower(filepath.Ext(filePath))
	switch ext {
	case ".ogg":
		return "audio/ogg" // WhatsApp prefers simple MIME type for voice messages
	case ".opus":
		return "audio/ogg" // Treat opus as ogg for WhatsApp compatibility
	case ".mp3":
		return "audio/mpeg"
	case ".wav":
		return "audio/wav"
	case ".m4a":
		return "audio/mp4"
	case ".aac":
		return "audio/aac"
	case ".flac":
		return "audio/flac"
	case ".wma":
		return "audio/x-ms-wma"
	case ".mp4":
		return "audio/mp4"
	case ".3gp":
		return "audio/3gpp"
	case ".amr":
		return "audio/amr"
	default:
		return "audio/ogg" // Default fallback for voice messages
	}
}

// getAudioDuration gets the duration of an audio file using ffprobe
func getAudioDuration(filePath string) (float64, error) {
	// Use ffprobe to get audio duration
	cmd := exec.Command("ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", filePath)
	output, err := cmd.Output()
	if err != nil {
		return 0, fmt.Errorf("failed to run ffprobe: %w", err)
	}

	// Parse JSON output
	var probeResult struct {
		Format struct {
			Duration string `json:"duration"`
		} `json:"format"`
	}

	if err := json.Unmarshal(output, &probeResult); err != nil {
		return 0, fmt.Errorf("failed to parse ffprobe output: %w", err)
	}

	// Convert duration string to float64
	duration, err := strconv.ParseFloat(probeResult.Format.Duration, 64)
	if err != nil {
		return 0, fmt.Errorf("failed to parse duration: %w", err)
	}

	return duration, nil
}

// DownloadMedia downloads media from a WhatsApp message
func (c *Client) DownloadMedia(messageID string) (string, error) {
	// Get message from database
	msg, err := c.db.GetMessageByID(messageID)
	if err != nil {
		return "", fmt.Errorf("message not found: %w", err)
	}

	if msg.MediaType == "" {
		return "", fmt.Errorf("message has no media")
	}

	// For now, return a placeholder path
	// In a real implementation, you would need to store the actual media data
	// and provide a way to retrieve it
	filename := fmt.Sprintf("%s_%s", messageID, msg.Filename)
	filePath := filepath.Join(c.mediaDir, filename)

	return filePath, nil
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}

// isNumeric checks if a string contains only numeric characters
func isNumeric(s string) bool {
	for _, char := range s {
		if char < '0' || char > '9' {
			return false
		}
	}
	return len(s) > 0
}

// shouldKeepTempAudioFiles checks if temporary audio files should be kept based on environment variable
func shouldKeepTempAudioFiles() bool {
	value := os.Getenv("KEEP_TEMP_AUDIO_FILES")
	if value == "" {
		return true
	}
	keep, err := strconv.ParseBool(value)
	if err != nil {
		return true
	}
	return keep
}

// VoiceChatResponse represents the response from voice-api-server /api/voice/chat endpoint
type VoiceChatResponse struct {
	UserInput          string `json:"user_input"`
	AgentResponse      string `json:"agent_response"`
	ConversationLength int    `json:"conversation_length"`
}

// processTextMessage handles text message processing (commands, auto-replies, etc.)
func (c *Client) processTextMessage(evt *events.Message, content string) {
	info := evt.Info
	// Skip processing messages from ourselves
	if info.IsFromMe {
		return
	}

	// Convert to lowercase for command matching
	lowerContent := strings.ToLower(strings.TrimSpace(content))

	// Example command handling
	switch {
	case strings.HasPrefix(lowerContent, "/help"):
		c.sendAutoReply(info.Chat.String(), "Available commands:\n/help - Show this help\n/ping - Test connection\n/time - Get current time")
	case strings.HasPrefix(lowerContent, "/ping"):
		c.sendAutoReply(info.Chat.String(), "Pong! 🏓")
	case strings.HasPrefix(lowerContent, "/time"):
		currentTime := time.Now().Format("2006-01-02 15:04:05")
		c.sendAutoReply(info.Chat.String(), fmt.Sprintf("Current time: %s", currentTime))
	case strings.Contains(lowerContent, "hello") || strings.Contains(lowerContent, "hi"):
		c.sendAutoReply(info.Chat.String(), "Hello! 👋 How can I help you?")
	default:
		// No specific command matched, use voice-api-server to generate response
		log.Printf("💬 Text message processed: %s", content)
		c.processWithVoiceAPI(info.Chat.String(), content)
	}
}

// processWithVoiceAPI processes text message using voice-api-server /api/text/chat endpoint
func (c *Client) processWithVoiceAPI(chatJID, content string) {
	log.Printf("🤖 Processing text message with voice-api-server: %s", content)

	response, err := c.callTextAPIChat(content)
	if err != nil {
		log.Printf("❌ Failed to process with voice-api-server: %v", err)
		c.sendAutoReply(chatJID, "Sorry, I'm having trouble processing your message right now. Please try again later.")
		return
	}

	log.Printf("✅ AI agent response: %s", response.AgentResponse)
	c.sendAutoReply(chatJID, response.AgentResponse)
}

// callTextAPIChat calls the voice-api-server /api/text/chat endpoint for text messages
func (c *Client) callTextAPIChat(text string) (*VoiceChatResponse, error) {
	log.Printf("📞 Calling voice-api-server /api/text/chat with text: %s", text)

	// Create JSON request body
	requestBody := map[string]string{
		"text": text,
	}
	jsonData, err := json.Marshal(requestBody)
	if err != nil {
		return nil, fmt.Errorf("failed to marshal request: %w", err)
	}

	// Create request with JSON data
	url := fmt.Sprintf("%s/api/text/chat", c.voiceAPIBaseURL)
	req, err := http.NewRequest("POST", url, bytes.NewBuffer(jsonData))
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", "application/json")

	// Send request
	resp, err := c.httpClient.Do(req)
	if err != nil {
		log.Printf("❌ Failed to send request to voice-api-server: %v", err)
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		log.Printf("❌ Voice-api-server returned status %d: %s", resp.StatusCode, string(body))
		return nil, fmt.Errorf("voice-api-server returned status %d: %s", resp.StatusCode, string(body))
	}

	// Parse response
	var chatResponse VoiceChatResponse
	if err := json.NewDecoder(resp.Body).Decode(&chatResponse); err != nil {
		log.Printf("❌ Failed to decode response: %v", err)
		return nil, fmt.Errorf("failed to decode response: %w", err)
	}

	log.Printf("✅ Voice-api-server response received successfully")
	return &chatResponse, nil
}

// callVoiceAPISpeak calls the voice-api-server /api/voice/speak endpoint to convert text to audio
func (c *Client) callVoiceAPISpeak(text string) ([]byte, error) {
	log.Printf("🔊 Calling voice-api-server /api/voice/speak with text: %s", text)

	// Create request URL with text query parameter
	url := fmt.Sprintf("%s/api/voice/speak?text=%s", c.voiceAPIBaseURL, url.QueryEscape(text))
	req, err := http.NewRequest("POST", url, nil)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}

	// Send request
	resp, err := c.httpClient.Do(req)
	if err != nil {
		log.Printf("❌ Failed to send request to voice-api-server: %v", err)
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		log.Printf("❌ Voice-api-server returned status %d: %s", resp.StatusCode, string(body))
		return nil, fmt.Errorf("voice-api-server returned status %d: %s", resp.StatusCode, string(body))
	}

	// Read audio data (WAV format)
	audioData, err := io.ReadAll(resp.Body)
	if err != nil {
		log.Printf("❌ Failed to read audio response: %v", err)
		return nil, fmt.Errorf("failed to read audio response: %w", err)
	}

	log.Printf("✅ Audio response received: %d bytes", len(audioData))
	return audioData, nil
}

// processAudioMessage handles audio/voice message processing
func (c *Client) processAudioMessage(evt *events.Message, audioMsg *waE2E.AudioMessage, messageType string) {
	info := evt.Info
	// Skip processing messages from ourselves
	if info.IsFromMe {
		return
	}

	log.Printf("🎵 Processing %s message from %s", messageType, info.Sender.String())

	// Different handling for voice vs regular audio
	if messageType == "voice" {
		log.Printf("🎤 Voice message received - processing with AI agent")
		c.processVoiceMessage(evt, audioMsg)
	} else {
		log.Printf("🎵 Regular audio message received - could trigger audio analysis")
		// TODO: Add audio analysis logic here
	}
}

// VoiceCompleteResponse represents the response from voice-api-server /api/voice/complete endpoint
type VoiceCompleteResponse struct {
	Transcript string `json:"transcript"`
	AgentText  string `json:"agent_text"`
	WavBase64  string `json:"wav_base64"`
}

// processVoiceMessage handles the complete voice message processing pipeline using voice-api-server
func (c *Client) processVoiceMessage(evt *events.Message, audioMsg *waE2E.AudioMessage) {
	info := evt.Info

	log.Printf("🎤 Starting voice message processing pipeline")

	// Step 0: Set voice recording presence to indicate we're processing
	if err := c.setVoiceRecordingPresence(info.Chat.String()); err != nil {
		log.Printf("⚠️ Failed to set voice recording presence: %v", err)
	}

	// Step 1: Download the voice message
	audioFilePath, err := c.downloadVoiceMessage(evt, audioMsg)
	if err != nil {
		log.Printf("❌ Failed to download voice message: %v", err)
		c.clearChatPresence(info.Chat.String()) // Clear presence on error
		c.sendAutoReply(info.Chat.String(), "Sorry, I couldn't download your voice message. Please try again.")
		return
	}
	if !shouldKeepTempAudioFiles() {
		defer os.Remove(audioFilePath) // Clean up downloaded file
	}

	log.Printf("✅ Voice message downloaded to: %s", audioFilePath)

	// Step 2: Convert OGG to WAV if enabled (default: enabled)
	// This ensures we send WAV format to voice-api-server
	finalAudioPath := audioFilePath
	if c.convertOggToWav {
		// Check if file is OGG/Opus format
		ext := strings.ToLower(filepath.Ext(audioFilePath))
		if ext == ".ogg" || ext == ".opus" {
			log.Printf("🔄 Converting OGG to WAV format: %s", audioFilePath)
			wavPath, convertErr := c.convertOggToWavFile(audioFilePath)
			if convertErr != nil {
				log.Printf("⚠️ Failed to convert OGG to WAV: %v (using original file)", convertErr)
				// Continue with original file if conversion fails
			} else {
				finalAudioPath = wavPath
				log.Printf("✅ Converted to WAV: %s", wavPath)
				// Clean up converted file after processing
				if !shouldKeepTempAudioFiles() {
					defer os.Remove(wavPath)
				}
			}
		}
	} else {
		log.Printf("ℹ️ OGG to WAV conversion disabled, using original file format")
	}

	// Step 3: Clear conversation history to ensure fresh query (matches UI behavior)
	// This ensures each voice message is processed independently
	if err := c.clearVoiceConversation(); err != nil {
		log.Printf("⚠️ Failed to clear conversation history: %v (continuing anyway)", err)
	}

	// Step 4: Call voice-api-server /api/voice/complete endpoint
	response, err := c.callVoiceAPIComplete(finalAudioPath)
	if err != nil {
		log.Printf("❌ Failed to process voice message with voice-api-server: %v", err)
		c.clearChatPresence(info.Chat.String()) // Clear presence on error
		c.sendAutoReply(info.Chat.String(), "Sorry, I'm having trouble processing your voice message right now. Please try again later.")
		return
	}

	log.Printf("✅ Voice transcribed: %s", response.Transcript)
	log.Printf("✅ AI agent response: %s", response.AgentText)

	// Step 4: Get TTS audio from voice-api-server
	// Matching UI: agent_text = response_data.get("agent_text", "")
	//              tts_audio = text_to_speech(agent_text)
	//              tts_base64 = base64.b64encode(tts_audio).decode("ascii") if tts_audio else ""
	// The UI calls /api/voice/speak separately, but voice-api-server /api/voice/complete already returns wav_base64
	// We'll use the wav_base64 from the complete response (which is what the voice-api-server generates)
	// This matches the UI's process but uses the audio already generated by the complete endpoint

	var audioData []byte

	if response.WavBase64 != "" {
		// Use the audio from the complete endpoint (matches what voice-api-server generates)
		// Decode base64 audio (matching UI: base64.b64encode().decode("ascii"))
		decodedAudio, decodeErr := base64.StdEncoding.DecodeString(response.WavBase64)
		if decodeErr != nil {
			log.Printf("❌ Failed to decode audio response: %v", decodeErr)
			c.clearChatPresence(info.Chat.String())
			c.sendAutoReply(info.Chat.String(), response.AgentText)
			return
		}
		audioData = decodedAudio
		log.Printf("✅ Decoded audio response from complete endpoint: %d bytes", len(audioData))
	} else {
		// Fallback: call /api/voice/speak separately (matching UI's text_to_speech() call)
		log.Printf("⚠️ No audio in complete response, calling /api/voice/speak separately (matching UI behavior)")
		speakAudio, speakErr := c.callVoiceAPISpeak(response.AgentText)
		if speakErr != nil {
			log.Printf("❌ Failed to get TTS audio: %v", speakErr)
			c.clearChatPresence(info.Chat.String())
			c.sendAutoReply(info.Chat.String(), response.AgentText)
			return
		}
		audioData = speakAudio
		log.Printf("✅ Got TTS audio from speak endpoint: %d bytes", len(audioData))
	}

	// Save decoded audio to temporary file (matching UI: saves to output.wav for compatibility)
	tempAudioPath := filepath.Join(c.mediaDir, fmt.Sprintf("response_%d.wav", time.Now().Unix()))
	if err := os.WriteFile(tempAudioPath, audioData, 0644); err != nil {
		log.Printf("❌ Failed to save audio response: %v", err)
		c.clearChatPresence(info.Chat.String())
		c.sendAutoReply(info.Chat.String(), response.AgentText)
		return
	}
	if !shouldKeepTempAudioFiles() {
		defer os.Remove(tempAudioPath)
	}

	log.Printf("✅ Saved audio response to: %s", tempAudioPath)

	// Convert WAV to OGG if needed (WhatsApp prefers OGG)
	oggPath, err := c.convertWavToOgg(tempAudioPath)
	if err != nil {
		log.Printf("⚠️ Failed to convert WAV to OGG, trying to send WAV: %v", err)
		oggPath = tempAudioPath
	} else {
		if !shouldKeepTempAudioFiles() {
			defer os.Remove(oggPath)
		}
	}

	// Step 5: Send audio response
	err = c.SendAudioMessage(info.Chat.String(), oggPath)
	if err != nil {
		log.Printf("❌ Failed to send audio response: %v", err)
		// Fallback to text response
		c.clearChatPresence(info.Chat.String())
		c.sendAutoReply(info.Chat.String(), response.AgentText)
		return
	}

	// Step 6: Clear voice recording presence
	if err := c.clearChatPresence(info.Chat.String()); err != nil {
		log.Printf("⚠️ Failed to clear chat presence: %v", err)
	}

	log.Printf("✅ Voice response sent successfully")
}

// callVoiceAPIComplete calls the voice-api-server /api/voice/complete endpoint
// This matches the UI implementation exactly: sends file as multipart/form-data with "file" field
func (c *Client) callVoiceAPIComplete(audioFilePath string) (*VoiceCompleteResponse, error) {
	log.Printf("📞 Calling voice-api-server /api/voice/complete with file: %s", audioFilePath)

	// Read audio file
	audioData, err := os.ReadFile(audioFilePath)
	if err != nil {
		return nil, fmt.Errorf("failed to read audio file: %w", err)
	}

	// Create multipart form (matching UI: files = {"file": (audio_file.filename, audio_data, audio_file.content_type)})
	// The requests library in Python automatically sets Content-Type based on filename extension
	// The UI uses requests.post() with files parameter which creates multipart/form-data
	var requestBody bytes.Buffer
	writer := multipart.NewWriter(&requestBody)

	// Add file field with proper filename (matching UI implementation)
	// The requests library in Python automatically sets Content-Type based on filename
	filename := filepath.Base(audioFilePath)
	part, err := writer.CreateFormFile("file", filename)
	if err != nil {
		return nil, fmt.Errorf("failed to create form file: %w", err)
	}

	if _, err := part.Write(audioData); err != nil {
		return nil, fmt.Errorf("failed to write file data: %w", err)
	}

	if err := writer.Close(); err != nil {
		return nil, fmt.Errorf("failed to close multipart writer: %w", err)
	}

	// Create HTTP request (matching UI: requests.post(VOICE_API_ENDPOINTS["complete"], files=files))
	url := fmt.Sprintf("%s/api/voice/complete", c.voiceAPIBaseURL)
	req, err := http.NewRequest("POST", url, &requestBody)
	if err != nil {
		return nil, fmt.Errorf("failed to create request: %w", err)
	}
	req.Header.Set("Content-Type", writer.FormDataContentType())

	log.Printf("📤 Sending request to %s (Content-Type: %s, File: %s, Size: %d bytes)", url, writer.FormDataContentType(), filename, len(audioData))

	// Send request
	resp, err := c.httpClient.Do(req)
	if err != nil {
		return nil, fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		log.Printf("❌ Voice-api-server returned status %d: %s", resp.StatusCode, string(body))
		return nil, fmt.Errorf("voice-api-server returned status %d: %s", resp.StatusCode, string(body))
	}

	// Parse response (matching UI: response_data = response.json())
	var voiceResponse VoiceCompleteResponse
	if err := json.NewDecoder(resp.Body).Decode(&voiceResponse); err != nil {
		return nil, fmt.Errorf("failed to decode response: %w", err)
	}

	log.Printf("✅ Voice-api-server response received: transcript=%d chars, agent_text=%d chars, wav_base64=%d chars",
		len(voiceResponse.Transcript), len(voiceResponse.AgentText), len(voiceResponse.WavBase64))

	return &voiceResponse, nil
}

// convertOggToWavFile converts an OGG/Opus file to WAV format using ffmpeg
func (c *Client) convertOggToWavFile(oggPath string) (string, error) {
	// Create output path in the same directory as input
	fileDir := filepath.Dir(oggPath)
	fileName := strings.TrimSuffix(filepath.Base(oggPath), filepath.Ext(oggPath))
	timestamp := time.Now().Unix()
	wavPath := filepath.Join(fileDir, fmt.Sprintf("%d_converted_%s.wav", timestamp, fileName))

	// Use ffmpeg to convert OGG/Opus to WAV
	// -y: overwrite output file if it exists
	// -i: input file
	// -ar 16000: sample rate 16kHz (common for speech)
	// -ac 1: mono channel
	// -sample_fmt s16: 16-bit PCM
	cmd := exec.Command("ffmpeg", "-y", "-i", oggPath, "-ar", "16000", "-ac", "1", "-sample_fmt", "s16", wavPath)

	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("ffmpeg conversion failed: %w", err)
	}

	return wavPath, nil
}

// convertWavToOgg converts a WAV file to OGG format using ffmpeg
func (c *Client) convertWavToOgg(wavPath string) (string, error) {
	oggPath := strings.TrimSuffix(wavPath, ".wav") + ".ogg"

	// Use ffmpeg to convert
	cmd := exec.Command("ffmpeg", "-y", "-i", wavPath, "-c:a", "libopus", "-b:a", "64k", "-ar", "48000", "-ac", "1", oggPath)
	if err := cmd.Run(); err != nil {
		return "", fmt.Errorf("ffmpeg conversion failed: %w", err)
	}

	return oggPath, nil
}

// downloadVoiceMessage downloads a voice message from WhatsApp
func (c *Client) downloadVoiceMessage(evt *events.Message, audioMsg *waE2E.AudioMessage) (string, error) {
	info := evt.Info

	log.Printf("📥 Downloading voice message from %s", info.Sender.String())

	// Create media directory if it doesn't exist
	if err := os.MkdirAll(c.mediaDir, 0755); err != nil {
		return "", fmt.Errorf("failed to create media directory: %w", err)
	}

	// Generate filename for the downloaded audio
	filename := fmt.Sprintf("voice_%s_%s.ogg", info.ID, time.Now().Format("20060102_150405"))
	filePath := filepath.Join(c.mediaDir, filename)

	// Download the media using WhatsApp client
	ctx := context.Background()
	data, err := c.client.Download(ctx, audioMsg)
	if err != nil {
		return "", fmt.Errorf("failed to download media: %w", err)
	}

	// Write the downloaded data to file
	file, err := os.Create(filePath)
	if err != nil {
		return "", fmt.Errorf("failed to create file: %w", err)
	}
	defer file.Close()

	_, err = file.Write(data)
	if err != nil {
		return "", fmt.Errorf("failed to write file: %w", err)
	}

	log.Printf("✅ Voice message downloaded successfully: %s", filePath)
	return filePath, nil
}

// sendAutoReply sends an automatic reply to a chat
func (c *Client) sendAutoReply(chatJID string, message string) {
	ctx := context.Background()
	if err := c.EnsureConnected(ctx); err != nil {
		log.Printf("❌ Failed to ensure connection for auto-reply: %v", err)
		return
	}

	recipientJID, err := types.ParseJID(chatJID)
	if err != nil {
		log.Printf("❌ Invalid chat JID for auto-reply: %v", err)
		return
	}

	msg := &waE2E.Message{
		Conversation: &message,
	}

	resp, err := c.client.SendMessage(ctx, recipientJID, msg)
	if err != nil {
		log.Printf("❌ Failed to send auto-reply: %v", err)
		return
	}

	// Store the auto-reply message in the database
	autoReplyMessage := &models.Message{
		Time:      time.Now(),
		Sender:    c.client.Store.ID.String(), // Our own JID
		Content:   message,
		IsFromMe:  true,
		MediaType: "text",
		Filename:  "",
		ChatJID:   chatJID,
		MessageID: resp.ID, // Use the actual message ID from WhatsApp response
	}

	if err := c.db.StoreMessage(autoReplyMessage); err != nil {
		log.Printf("⚠️ Failed to store auto-reply message in database: %v", err)
	} else {
		log.Printf("✅ Auto-reply message stored in database")
	}

	// Update chat info
	c.updateChatInfo(recipientJID, message, time.Now())

	log.Printf("✅ Auto-reply sent: %s", message)
}

// generateFallbackResponse generates a simple fallback response when voice-api-server is unavailable
func (c *Client) generateFallbackResponse(content string) string {
	lowerContent := strings.ToLower(strings.TrimSpace(content))

	// Simple keyword-based responses
	switch {
	case strings.Contains(lowerContent, "hello") || strings.Contains(lowerContent, "hi"):
		return "Hello! 👋 I'm here to help you with WhatsApp. How can I assist you today?"
	case strings.Contains(lowerContent, "help"):
		return "I can help you with WhatsApp operations like:\n• Searching contacts\n• Managing messages\n• Sending files\n• Getting chat information\n\nWhat would you like to do?"
	case strings.Contains(lowerContent, "thank"):
		return "You're welcome! 😊 Is there anything else I can help you with?"
	case strings.Contains(lowerContent, "bye") || strings.Contains(lowerContent, "goodbye"):
		return "Goodbye! 👋 Feel free to reach out anytime you need help with WhatsApp."
	case strings.Contains(lowerContent, "time"):
		return fmt.Sprintf("The current time is: %s", time.Now().Format("2006-01-02 15:04:05"))
	case strings.Contains(lowerContent, "weather"):
		return "I don't have access to weather information right now, but I can help you with WhatsApp-related tasks!"
	case strings.Contains(lowerContent, "how are you"):
		return "I'm doing well, thank you for asking! 😊 I'm here and ready to help you with WhatsApp operations."
	default:
		return "I received your message! While my AI assistant is temporarily unavailable, I'm still here to help you with WhatsApp operations. You can ask me about contacts, messages, or other WhatsApp features."
	}
}

// setVoiceRecordingPresence sets the chat presence to indicate voice recording
func (c *Client) setVoiceRecordingPresence(chatJID string) error {
	ctx := context.Background()
	if err := c.EnsureConnected(ctx); err != nil {
		return fmt.Errorf("failed to ensure connection for presence: %w", err)
	}

	recipientJID, err := types.ParseJID(chatJID)
	if err != nil {
		return fmt.Errorf("invalid chat JID for presence: %w", err)
	}

	log.Printf("🎤 Setting voice recording presence for %s", chatJID)
	err = c.client.SendChatPresence(recipientJID, types.ChatPresenceComposing, types.ChatPresenceMediaAudio)
	if err != nil {
		log.Printf("❌ Failed to set voice recording presence: %v", err)
		return fmt.Errorf("failed to set voice recording presence: %w", err)
	}

	log.Printf("✅ Voice recording presence set successfully")
	return nil
}

// clearChatPresence clears the chat presence indicator
func (c *Client) clearChatPresence(chatJID string) error {
	ctx := context.Background()
	if err := c.EnsureConnected(ctx); err != nil {
		return fmt.Errorf("failed to ensure connection for presence: %w", err)
	}

	recipientJID, err := types.ParseJID(chatJID)
	if err != nil {
		return fmt.Errorf("invalid chat JID for presence: %w", err)
	}

	log.Printf("🔄 Clearing chat presence for %s", chatJID)
	err = c.client.SendChatPresence(recipientJID, types.ChatPresencePaused, "")
	if err != nil {
		log.Printf("❌ Failed to clear chat presence: %v", err)
		return fmt.Errorf("failed to clear chat presence: %w", err)
	}

	log.Printf("✅ Chat presence cleared successfully")
	return nil
}

// clearVoiceConversation clears the conversation history in voice-api-server
// This ensures each voice message is processed as a fresh, independent query
func (c *Client) clearVoiceConversation() error {
	log.Printf("🔄 Clearing voice conversation history")

	url := fmt.Sprintf("%s/api/voice/conversation/clear", c.voiceAPIBaseURL)
	req, err := http.NewRequest("POST", url, nil)
	if err != nil {
		return fmt.Errorf("failed to create request: %w", err)
	}

	resp, err := c.httpClient.Do(req)
	if err != nil {
		log.Printf("⚠️ Failed to clear conversation: %v", err)
		return fmt.Errorf("failed to send request: %w", err)
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		body, _ := io.ReadAll(resp.Body)
		log.Printf("⚠️ Failed to clear conversation: status %d, body: %s", resp.StatusCode, string(body))
		return fmt.Errorf("voice-api-server returned status %d: %s", resp.StatusCode, string(body))
	}

	log.Printf("✅ Voice conversation history cleared successfully")
	return nil
}
