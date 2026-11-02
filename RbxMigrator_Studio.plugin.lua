--!strict
-- RbxMigrator - Studio Plugin
-- Designed for the RobloxTowerAnimationMigrator_Portal_FIXED desktop helper.
--
-- Features:
--   * One-click migration of the selected tower/model.
--   * Detects duplicate AnimationIds and migrates each unique asset only once.
--   * Tries Roblox Studio AnimationClipProvider first.
--   * Falls back to the desktop helper's Roblox Asset Delivery / RBXDL Portal flow.
--   * Caches old -> new IDs locally so duplicate towers can be fixed without re-uploading.
--   * "Force re-upload" mode ignores the cache.
--   * "Apply Cache" can fix another copy of a tower with zero network requests.
--   * Undo waypoints around replacements.
--   * Live progress, helper status, migration report, and detailed failures.
--   * Configurable localhost helper port.
--
-- IMPORTANT:
--   The desktop helper must be running.
--   Studio may ask once for permission to access http://127.0.0.1:<port>.

assert(plugin, "This script must run as a Roblox Studio plugin.")

local HttpService = game:GetService("HttpService")
local Selection = game:GetService("Selection")
local RunService = game:GetService("RunService")
local ChangeHistoryService = game:GetService("ChangeHistoryService")
local AnimationClipProvider = game:GetService("AnimationClipProvider")
local TweenService = game:GetService("TweenService")
local ScriptEditorService = game:GetService("ScriptEditorService")
local AssetService = game:GetService("AssetService")

----------------------------------------------------------------
-- Constants / settings
----------------------------------------------------------------

local DEFAULT_PORT = 8765
local POLL_INTERVAL = 1.0
local MAX_WAIT_SECONDS = 20 * 60

-- AnimationClipProvider work can run in parallel, but Roblox Studio's
-- HttpService has its own request/concurrency limits. We therefore do NOT
-- POST one localhost request per worker. Workers prepare source in parallel,
-- then a single reporter sends small batches to the helper.
local STUDIO_PREP_MAX_WORKERS = 12
local STUDIO_REPORT_BATCH_SIZE = 8
local HTTP_LIMIT_RETRIES = 6
local HTTP_LIMIT_RETRY_BASE = 0.25
-- Failed unique assets are retried separately so successful uploads are not repeated.
local MAX_FAILED_ASSET_RETRIES = 2

local CACHE_SETTING = "TowerAnimationMigrator.MappingCache.v2"
local PORT_SETTING = "TowerAnimationMigrator.Port"
local LAST_REPORT_SETTING = "TowerAnimationMigrator.LastReport"

-- Roblox caps the Text property at 200,000 characters; keep reports under that.
local MAX_REPORT_CHARS = 200000

local port = plugin:GetSetting(PORT_SETTING)

if type(port) ~= "number" or port < 1 or port > 65535 then
	port = DEFAULT_PORT
	plugin:SetSetting(PORT_SETTING, port)
end

local function baseUrl(): string
	return "http://127.0.0.1:" .. tostring(port)
end

----------------------------------------------------------------
-- Mapping cache
----------------------------------------------------------------

type Mapping = {[string]: string}

local function loadCache(): Mapping
	local raw = plugin:GetSetting(CACHE_SETTING)

	if type(raw) ~= "string" or raw == "" then
		return {}
	end

	local ok, decoded = pcall(function()
		return HttpService:JSONDecode(raw)
	end)

	if not ok or type(decoded) ~= "table" then
		return {}
	end

	local result: Mapping = {}

	for oldId, newId in pairs(decoded) do
		if type(oldId) == "string"
			and type(newId) == "string"
			and (
				oldId:match("^%d+$")
					or oldId:match("^Animation:%d+$")
					or oldId:match("^Audio:%d+$")
					or oldId:match("^Mesh:%d+$")
			)
				and newId:match("^%d+$")
		then
			result[oldId] = newId
		end
	end

	return result
end

local function saveCache(cache: Mapping)
	local ok, encoded = pcall(function()
		return HttpService:JSONEncode(cache)
	end)

	if ok then
		plugin:SetSetting(CACHE_SETTING, encoded)
	end
end

local mappingCache: Mapping = loadCache()

local enabledAssetTypes: {[string]: boolean} = {
	Animation = true,
	Audio = false,
	Mesh = false,
}

local ASSET_TYPE_ORDER = { "Animation", "Audio", "Mesh" }

local ASSET_TYPE_LABELS: {[string]: string} = {
	Animation = "Animations",
	Audio = "Sounds",
	Mesh = "Meshes",
}

-- Distinct asset classes must NEVER share an untyped mapping entry.
local function cacheKey(assetType: string, assetId: string): string
	return assetType .. ":" .. assetId
end

local function cachedNewId(assetType: string, assetId: string): string?
	return mappingCache[cacheKey(assetType, assetId)]
		or (if assetType ~= "Mesh" then mappingCache[assetId] else nil)
end

----------------------------------------------------------------
-- Toolbar
----------------------------------------------------------------

local toolbar = plugin:CreateToolbar("RbxMigrator")

-- The toolbar button ONLY opens/closes the window.
-- It never starts a migration.
local toolbarButton = toolbar:CreateButton(
	"TowerAssetMigrator",
	"Open the RbxMigrator.",
	"",
	"RbxMigrator"
)

toolbarButton.ClickableWhenViewportHidden = true

----------------------------------------------------------------
-- Dock widget
----------------------------------------------------------------

local widgetInfo = DockWidgetPluginGuiInfo.new(
	Enum.InitialDockState.Float,
	false,
	false,
	580,
	660,
	450,
	470
)

local widget = plugin:CreateDockWidgetPluginGuiAsync(
	"TowerAssetMigratorPro",
	widgetInfo
)

widget.Title = "RbxMigrator"

local rootFrame = Instance.new("Frame")
rootFrame.Name = "Root"
rootFrame.Size = UDim2.fromScale(1, 1)
rootFrame.BorderSizePixel = 0
rootFrame.BackgroundColor3 = Color3.fromRGB(31, 31, 31)
rootFrame.Parent = widget

-- Scales the whole panel in when the widget opens.
local rootScale = Instance.new("UIScale")
rootScale.Parent = rootFrame

local padding = Instance.new("UIPadding")
padding.PaddingLeft = UDim.new(0, 12)
padding.PaddingRight = UDim.new(0, 12)
padding.PaddingTop = UDim.new(0, 12)
padding.PaddingBottom = UDim.new(0, 12)
padding.Parent = rootFrame

local layout = Instance.new("UIListLayout")
layout.FillDirection = Enum.FillDirection.Vertical
layout.SortOrder = Enum.SortOrder.LayoutOrder
layout.Padding = UDim.new(0, 8)
layout.Parent = rootFrame

local function makeLabel(
	text: string,
	height: number,
	textSize: number?,
	bold: boolean?
): TextLabel
	local label = Instance.new("TextLabel")
	label.Size = UDim2.new(1, 0, 0, height)
	label.BackgroundTransparency = 1
	label.Text = text
	label.TextColor3 = Color3.fromRGB(235, 235, 235)
	label.TextXAlignment = Enum.TextXAlignment.Left
	label.TextYAlignment = Enum.TextYAlignment.Center
	label.TextWrapped = true
	label.Font = if bold then Enum.Font.SourceSansSemibold else Enum.Font.SourceSans
	label.TextSize = textSize or 16
	return label
end

local titleLabel = makeLabel("RbxMigrator", 30, 22, true)
titleLabel.LayoutOrder = 1
titleLabel.Parent = rootFrame

-- Pulsing glow behind the title.
local titleStroke = Instance.new("UIStroke")
titleStroke.Color = Color3.fromRGB(0, 162, 255)
titleStroke.Thickness = 1.4
titleStroke.Transparency = 0.85
titleStroke.Parent = titleLabel

TweenService:Create(
	titleStroke,
	TweenInfo.new(1.6, Enum.EasingStyle.Sine, Enum.EasingDirection.InOut, -1, true),
	{ Transparency = 0.45 }
):Play()

local selectionLabel = makeLabel("Selection: none", 44, 16, false)
selectionLabel.LayoutOrder = 2
selectionLabel.TextColor3 = Color3.fromRGB(195, 195, 195)
selectionLabel.Parent = rootFrame

local helperRow = Instance.new("Frame")
helperRow.Size = UDim2.new(1, 0, 0, 32)
helperRow.BackgroundTransparency = 1
helperRow.LayoutOrder = 3
helperRow.Parent = rootFrame

local helperLayout = Instance.new("UIListLayout")
helperLayout.FillDirection = Enum.FillDirection.Horizontal
helperLayout.VerticalAlignment = Enum.VerticalAlignment.Center
helperLayout.Padding = UDim.new(0, 6)
helperLayout.Parent = helperRow

local helperStatusLabel = makeLabel("Helper: not checked", 32, 14, false)
helperStatusLabel.Size = UDim2.new(1, -190, 1, 0)
helperStatusLabel.TextColor3 = Color3.fromRGB(190, 190, 190)
helperStatusLabel.Parent = helperRow

local portBox = Instance.new("TextBox")
portBox.Size = UDim2.fromOffset(72, 27)
portBox.BackgroundColor3 = Color3.fromRGB(47, 47, 47)
portBox.BorderSizePixel = 0
portBox.TextColor3 = Color3.fromRGB(240, 240, 240)
portBox.PlaceholderText = "Port"
portBox.ClearTextOnFocus = false
portBox.Text = tostring(port)
portBox.Font = Enum.Font.Code
portBox.TextSize = 14
portBox.Parent = helperRow

local portCorner = Instance.new("UICorner")
portCorner.CornerRadius = UDim.new(0, 5)
portCorner.Parent = portBox

local checkButton = Instance.new("TextButton")
checkButton.Size = UDim2.fromOffset(105, 27)
checkButton.BackgroundColor3 = Color3.fromRGB(55, 55, 55)
checkButton.BorderSizePixel = 0
checkButton.Text = "Check Helper"
checkButton.TextColor3 = Color3.fromRGB(240, 240, 240)
checkButton.Font = Enum.Font.SourceSansSemibold
checkButton.TextSize = 14
checkButton.Parent = helperRow

local checkCorner = Instance.new("UICorner")
checkCorner.CornerRadius = UDim.new(0, 5)
checkCorner.Parent = checkButton

local optionRow = Instance.new("Frame")
optionRow.Size = UDim2.new(1, 0, 0, 30)
optionRow.BackgroundTransparency = 1
optionRow.LayoutOrder = 4
optionRow.Parent = rootFrame

local forceButton = Instance.new("TextButton")
forceButton.Size = UDim2.fromOffset(220, 28)
forceButton.BackgroundColor3 = Color3.fromRGB(48, 48, 48)
forceButton.BorderSizePixel = 0
forceButton.TextColor3 = Color3.fromRGB(235, 235, 235)
forceButton.Font = Enum.Font.SourceSans
forceButton.TextSize = 15
forceButton.Text = "â Force re-upload (ignore cache)"
forceButton.Parent = optionRow

local forceCorner = Instance.new("UICorner")
forceCorner.CornerRadius = UDim.new(0, 5)
forceCorner.Parent = forceButton

local forceReupload = false

local assetTypeRow = Instance.new("Frame")
assetTypeRow.Size = UDim2.new(1, 0, 0, 34)
assetTypeRow.BackgroundTransparency = 1
assetTypeRow.LayoutOrder = 5
assetTypeRow.Parent = rootFrame

local assetTypeLayout = Instance.new("UIListLayout")
assetTypeLayout.FillDirection = Enum.FillDirection.Horizontal
assetTypeLayout.VerticalAlignment = Enum.VerticalAlignment.Center
assetTypeLayout.Padding = UDim.new(0, 7)
assetTypeLayout.Parent = assetTypeRow

local assetTypeCaption = makeLabel("Types:", 30, 14, true)
assetTypeCaption.Size = UDim2.fromOffset(48, 30)
assetTypeCaption.TextColor3 = Color3.fromRGB(190, 190, 190)
assetTypeCaption.Parent = assetTypeRow

local animationTypeButton = Instance.new("TextButton")
animationTypeButton.Size = UDim2.fromOffset(150, 28)
animationTypeButton.BorderSizePixel = 0
animationTypeButton.TextColor3 = Color3.fromRGB(245, 245, 245)
animationTypeButton.Font = Enum.Font.SourceSansSemibold
animationTypeButton.TextSize = 14
animationTypeButton.Parent = assetTypeRow

local animationTypeCorner = Instance.new("UICorner")
animationTypeCorner.CornerRadius = UDim.new(0, 5)
animationTypeCorner.Parent = animationTypeButton

local soundTypeButton = Instance.new("TextButton")
soundTypeButton.Size = UDim2.fromOffset(130, 28)
soundTypeButton.BorderSizePixel = 0
soundTypeButton.TextColor3 = Color3.fromRGB(245, 245, 245)
soundTypeButton.Font = Enum.Font.SourceSansSemibold
soundTypeButton.TextSize = 14
soundTypeButton.Parent = assetTypeRow

local soundTypeCorner = Instance.new("UICorner")
soundTypeCorner.CornerRadius = UDim.new(0, 5)
soundTypeCorner.Parent = soundTypeButton

local meshTypeButton = Instance.new("TextButton")
meshTypeButton.Size = UDim2.fromOffset(120, 28)
meshTypeButton.BorderSizePixel = 0
meshTypeButton.TextColor3 = Color3.fromRGB(245, 245, 245)
meshTypeButton.Font = Enum.Font.SourceSansSemibold
meshTypeButton.TextSize = 14
meshTypeButton.Parent = assetTypeRow

local meshTypeCorner = Instance.new("UICorner")
meshTypeCorner.CornerRadius = UDim.new(0, 5)
meshTypeCorner.Parent = meshTypeButton

local statusLabel = makeLabel("Ready", 28, 18, true)
statusLabel.LayoutOrder = 6
statusLabel.Parent = rootFrame

local detailLabel = makeLabel(
	"Select a folder/model/asset, choose types, then click Migrate Selected.",
	54,
	15,
	false
)
detailLabel.LayoutOrder = 7
detailLabel.TextColor3 = Color3.fromRGB(195, 195, 195)
detailLabel.TextYAlignment = Enum.TextYAlignment.Top
detailLabel.Parent = rootFrame

local progressBackground = Instance.new("Frame")
progressBackground.Size = UDim2.new(1, 0, 0, 10)
progressBackground.BackgroundColor3 = Color3.fromRGB(48, 48, 48)
progressBackground.BorderSizePixel = 0
progressBackground.LayoutOrder = 8
progressBackground.ClipsDescendants = true
progressBackground.Parent = rootFrame

local progressBackgroundCorner = Instance.new("UICorner")
progressBackgroundCorner.CornerRadius = UDim.new(1, 0)
progressBackgroundCorner.Parent = progressBackground

local progressFill = Instance.new("Frame")
progressFill.Size = UDim2.new(0, 0, 1, 0)
progressFill.BackgroundColor3 = Color3.fromRGB(0, 162, 255)
progressFill.BorderSizePixel = 0
progressFill.Parent = progressBackground

local progressFillCorner = Instance.new("UICorner")
progressFillCorner.CornerRadius = UDim.new(1, 0)
progressFillCorner.Parent = progressFill

-- Moving shimmer across the progress bar while a migration runs.
local progressGradient = Instance.new("UIGradient")
progressGradient.Color = ColorSequence.new({
	ColorSequenceKeypoint.new(0, Color3.fromRGB(0, 130, 255)),
	ColorSequenceKeypoint.new(0.5, Color3.fromRGB(130, 220, 255)),
	ColorSequenceKeypoint.new(1, Color3.fromRGB(0, 130, 255)),
})
progressGradient.Rotation = 20
progressGradient.Offset = Vector2.new(-1, 0)
progressGradient.Parent = progressFill

local progressShimmer = TweenService:Create(
	progressGradient,
	TweenInfo.new(1.1, Enum.EasingStyle.Sine, Enum.EasingDirection.InOut, -1, false),
	{ Offset = Vector2.new(1, 0) }
)

local buttonRow = Instance.new("Frame")
buttonRow.Size = UDim2.new(1, 0, 0, 36)
buttonRow.BackgroundTransparency = 1
buttonRow.LayoutOrder = 9
buttonRow.Parent = rootFrame

local buttonLayout = Instance.new("UIListLayout")
buttonLayout.FillDirection = Enum.FillDirection.Horizontal
buttonLayout.Padding = UDim.new(0, 7)
buttonLayout.Parent = buttonRow

local function makeButton(text: string, width: number): TextButton
	local button = Instance.new("TextButton")
	button.Size = UDim2.fromOffset(width, 34)
	button.BackgroundColor3 = Color3.fromRGB(55, 55, 55)
	button.BorderSizePixel = 0
	button.Text = text
	button.TextColor3 = Color3.fromRGB(245, 245, 245)
	button.Font = Enum.Font.SourceSansSemibold
	button.TextSize = 15

	local corner = Instance.new("UICorner")
	corner.CornerRadius = UDim.new(0, 6)
	corner.Parent = button

	return button
end

local function attachButtonAnimation(button: TextButton)
	local stroke = Instance.new("UIStroke")
	stroke.Color = Color3.fromRGB(255, 255, 255)
	stroke.Thickness = 1
	stroke.Transparency = 1
	stroke.Parent = button

	local scale = Instance.new("UIScale")
	scale.Name = "ButtonScale"
	scale.Parent = button

	local function tweenScale(value: number, easingStyle: Enum.EasingStyle, duration: number)
		TweenService:Create(
			scale,
			TweenInfo.new(duration, easingStyle, Enum.EasingDirection.Out),
			{ Scale = value }
		):Play()
	end

	button.MouseEnter:Connect(function()
		-- Capture the resting color each time so toggled buttons animate correctly.
		local resting = button.BackgroundColor3
		tweenScale(1.06, Enum.EasingStyle.Back, 0.14)
		TweenService:Create(
			stroke,
			TweenInfo.new(0.12, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
			{ Transparency = 0.68 }
		):Play()
		TweenService:Create(
			button,
			TweenInfo.new(0.12, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
			{ BackgroundColor3 = resting:Lerp(Color3.new(1, 1, 1), 0.08) }
		):Play()
	end)

	button.MouseLeave:Connect(function()
		tweenScale(1, Enum.EasingStyle.Quad, 0.16)
		TweenService:Create(
			stroke,
			TweenInfo.new(0.16, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
			{ Transparency = 1 }
		):Play()
	end)

	-- Squash on press, spring back on release.
	button.MouseButton1Down:Connect(function()
		tweenScale(0.94, Enum.EasingStyle.Quad, 0.08)
	end)

	button.MouseButton1Up:Connect(function()
		tweenScale(1.06, Enum.EasingStyle.Back, 0.14)
	end)
end

local migrateUiButton = makeButton("Migrate Selected", 128)
migrateUiButton.BackgroundColor3 = Color3.fromRGB(0, 112, 184)
migrateUiButton.Parent = buttonRow

local previewUiButton = makeButton("Preview / Dry Run", 125)
previewUiButton.Parent = buttonRow

local applyCacheButton = makeButton("Apply Cache", 90)
applyCacheButton.Parent = buttonRow

local clearCacheButton = makeButton("Clear Cache", 80)
clearCacheButton.Parent = buttonRow

local reportTitle = makeLabel("Migration report / dry-run preview", 24, 16, true)
reportTitle.LayoutOrder = 10
reportTitle.Parent = rootFrame

local reportBox = Instance.new("TextBox")
reportBox.Size = UDim2.new(1, 0, 1, -392)
reportBox.BackgroundColor3 = Color3.fromRGB(22, 22, 22)
reportBox.BorderSizePixel = 0
reportBox.TextColor3 = Color3.fromRGB(220, 220, 220)
reportBox.TextXAlignment = Enum.TextXAlignment.Left
reportBox.TextYAlignment = Enum.TextYAlignment.Top
reportBox.TextWrapped = false
reportBox.MultiLine = true
reportBox.ClearTextOnFocus = false
reportBox.TextEditable = false
reportBox.Font = Enum.Font.Code
reportBox.TextSize = 13
reportBox.Text = plugin:GetSetting(LAST_REPORT_SETTING) or "No migration has run yet."
reportBox.LayoutOrder = 11
reportBox.Parent = rootFrame

local reportPadding = Instance.new("UIPadding")
reportPadding.PaddingLeft = UDim.new(0, 8)
reportPadding.PaddingRight = UDim.new(0, 8)
reportPadding.PaddingTop = UDim.new(0, 8)
reportPadding.PaddingBottom = UDim.new(0, 8)
reportPadding.Parent = reportBox

local reportCorner = Instance.new("UICorner")
reportCorner.CornerRadius = UDim.new(0, 6)
reportCorner.Parent = reportBox

local footerRow = Instance.new("Frame")
footerRow.Size = UDim2.new(1, 0, 0, 32)
footerRow.BackgroundTransparency = 1
footerRow.LayoutOrder = 12
footerRow.Parent = rootFrame

local footerLayout = Instance.new("UIListLayout")
footerLayout.FillDirection = Enum.FillDirection.Horizontal
footerLayout.Padding = UDim.new(0, 7)
footerLayout.Parent = footerRow

local printReportButton = makeButton("Print Report", 105)
printReportButton.Parent = footerRow

local refreshSelectionButton = makeButton("Refresh Selection", 125)
refreshSelectionButton.Parent = footerRow

for _, button in ipairs({
	migrateUiButton,
	previewUiButton,
	applyCacheButton,
	clearCacheButton,
	printReportButton,
	refreshSelectionButton,
	checkButton,
	forceButton,
	animationTypeButton,
	soundTypeButton,
	meshTypeButton,
	}) do
	attachButtonAnimation(button)
end

----------------------------------------------------------------
-- UI state helpers
----------------------------------------------------------------

local progressTween: Tween? = nil

local function setProgress(value: number)
	local clamped = math.clamp(value, 0, 1)

	if progressTween then
		progressTween:Cancel()
	end

	progressTween = TweenService:Create(
		progressFill,
		TweenInfo.new(0.18, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
		{ Size = UDim2.new(clamped, 0, 1, 0) }
	)
	progressTween:Play()
end

local function setStatus(
	title: string,
	detail: string,
	isError: boolean?
)
	-- Fade the new status text in so updates feel alive.
	local function animateIn(label: TextLabel)
		label.TextTransparency = 0.45
		TweenService:Create(
			label,
			TweenInfo.new(0.28, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
			{ TextTransparency = 0 }
		):Play()
	end

	statusLabel.Text = title
	detailLabel.Text = detail
	animateIn(statusLabel)
	animateIn(detailLabel)

	local targetColor = Color3.fromRGB(245, 245, 245)

	if isError then
		targetColor = Color3.fromRGB(255, 105, 105)
	elseif title:lower():find("complete") then
		targetColor = Color3.fromRGB(100, 225, 135)
	elseif title:lower():find("upload") or title:lower():find("migrat") then
		targetColor = Color3.fromRGB(105, 190, 255)
	end

	TweenService:Create(
		statusLabel,
		TweenInfo.new(0.16, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
		{ TextColor3 = targetColor }
	):Play()
end

local function updateTypeButtons()
	local function style(button: TextButton, enabled: boolean, label: string)
		button.Text = (if enabled then "â " else "â ") .. label

		local color = if enabled
			then Color3.fromRGB(0, 112, 184)
			else Color3.fromRGB(48, 48, 48)

		TweenService:Create(
			button,
			TweenInfo.new(0.16, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
			{ BackgroundColor3 = color }
		):Play()

		-- Pop the button whenever its state flips.
		local pop = button:FindFirstChild("ButtonScale")
		if pop and pop:IsA("UIScale") then
			pop.Scale = 0.9
			TweenService:Create(
				pop,
				TweenInfo.new(0.22, Enum.EasingStyle.Back, Enum.EasingDirection.Out),
				{ Scale = 1 }
			):Play()
		end
	end

	style(animationTypeButton, enabledAssetTypes.Animation, "Animations")
	style(soundTypeButton, enabledAssetTypes.Audio, "Sounds")
	style(meshTypeButton, enabledAssetTypes.Mesh, "Meshes")
end

local function setReport(report: string)
	-- Roblox caps Text at 200,000 characters; truncate large reports and note the cut.
	if #report >= MAX_REPORT_CHARS then
		local shown = report:sub(1, MAX_REPORT_CHARS - 1)
		-- Try to cut at a line boundary so the report stays readable.
		local lastLine = shown:find("\n[^\n]*$")
		if lastLine then
			shown = shown:sub(1, lastLine)
		end
		report = shown .. "\n\n... report truncated (" .. tostring(#report) .. " chars total; showing first " .. tostring(#shown) .. ") ..."
	end
	reportBox.Text = report
	-- Some reports are too large for a plugin setting; don't interrupt scanning.
	pcall(function() plugin:SetSetting(LAST_REPORT_SETTING, report) end)
end

----------------------------------------------------------------
-- HTTP
----------------------------------------------------------------

local function request(
	method: string,
	path: string,
	body: any?
): any
	local requestData: {[string]: any} = {
		Url = baseUrl() .. path,
		Method = method,
		Headers = {
			["Accept"] = "application/json",
		},
	}

	if body ~= nil then
		requestData.Headers["Content-Type"] = "application/json"
		requestData.Body = HttpService:JSONEncode(body)
	end

	local ok = false
	local response: any = nil

	for attempt = 1, HTTP_LIMIT_RETRIES do
		ok, response = pcall(function()
			return HttpService:RequestAsync(requestData)
		end)

		if ok then
			break
		end

		local failureText = tostring(response)

		if string.find(
			failureText,
			"Number of requests exceeded limit",
			1,
			true
			) and attempt < HTTP_LIMIT_RETRIES then
			local delaySeconds = math.min(
				HTTP_LIMIT_RETRY_BASE * (2 ^ (attempt - 1)),
				2
			)

			task.wait(delaySeconds)
		else
			break
		end
	end

	if not ok then
		error(
			"Could not reach the desktop helper at "
				.. baseUrl()
				.. ". "
				.. tostring(response)
		)
	end

	if not response.Success then
		local message = ("%d %s"):format(
			response.StatusCode,
			response.StatusMessage
		)

		local decodedOk, decoded = pcall(
			HttpService.JSONDecode,
			HttpService,
			response.Body
		)

		if decodedOk
			and type(decoded) == "table"
			and decoded.error
		then
			message ..= ": " .. tostring(decoded.error)
		end

		error(message)
	end

	if response.Body == "" then
		return {}
	end

	return HttpService:JSONDecode(response.Body)
end

local function checkHelper(showSuccess: boolean?): boolean
	local ok, result = pcall(function()
		return request("GET", "/ping")
	end)

	if not ok then
		helperStatusLabel.Text = "Helper: offline"
		helperStatusLabel.TextColor3 = Color3.fromRGB(255, 110, 110)

		if showSuccess then
			setStatus("Helper offline", tostring(result), true)
		end

		return false
	end

	if result.configured then
		helperStatusLabel.Text =
			"Helper: ready (" .. tostring(result.creatorType or "?") .. ")"
		helperStatusLabel.TextColor3 = Color3.fromRGB(105, 225, 135)

		if showSuccess then
			setStatus(
				"Helper ready",
				"Connected to " .. baseUrl() .. "."
			)
		end

		return true
	end

	helperStatusLabel.Text = "Helper: needs setup"
	helperStatusLabel.TextColor3 = Color3.fromRGB(255, 195, 90)

	if showSuccess then
		setStatus(
			"Helper needs setup",
			"Open the desktop helper and configure its API key and creator ID.",
			true
		)
	end

	return false
end

----------------------------------------------------------------
-- Selection / manifest
----------------------------------------------------------------

type AssetManifestEntry = {
	name: string,
	path: string,
	relativePath: string,
	assetId: string,
	assetType: string,
	referenceKind: string?,
	modulePath: string?,
	fieldPath: string?,
	propertyName: string?,
	valueStart: number?,
	valueEnd: number?, -- exclusive index, Luau substring convention
	supported: boolean?,
	note: string?,
}

type Manifest = {
	tower: string,
	rootPath: string,
	assetType: string,
	assets: {AssetManifestEntry},
}

local function relativePath(
	root: Instance,
	object: Instance
): string
	local parts = {}
	local current: Instance? = object

	while current and current ~= root do
		table.insert(parts, 1, current.Name)
		current = current.Parent
	end

	if object == root then
		return object.Name
	end

	return table.concat(parts, ".")
end

local function instancesIncludingRoot(root: Instance): {Instance}
	-- GetDescendants already allocates the array; append the root instead
	-- of copying every descendant into a second table.
	local result = root:GetDescendants()
	table.insert(result, root)
	return result
end

-- Scan native instance Content properties without changing them. Only the
-- Animation/Audio/Mesh entries can be uploaded by RbxMigrator. Image/Video are
-- surfaced in dry-run reports but deliberately NOT added to migration jobs.
local PROPERTY_REGISTRY: {{className: string, property: string, assetType: string, supported: boolean}} = {
	{className = "Animation", property = "AnimationId", assetType = "Animation", supported = true},
	{className = "Sound", property = "SoundId", assetType = "Audio", supported = true},
	{className = "MeshPart", property = "MeshId", assetType = "Mesh", supported = true},
	{className = "SpecialMesh", property = "MeshId", assetType = "Mesh", supported = true},
	{className = "MeshPart", property = "TextureID", assetType = "Image", supported = false},
	{className = "SpecialMesh", property = "TextureId", assetType = "Image", supported = false},
	{className = "Decal", property = "Texture", assetType = "Image", supported = false},
	{className = "Texture", property = "Texture", assetType = "Image", supported = false},
	{className = "ImageLabel", property = "Image", assetType = "Image", supported = false},
	{className = "ImageButton", property = "Image", assetType = "Image", supported = false},
	{className = "ParticleEmitter", property = "Texture", assetType = "Image", supported = false},
	{className = "Beam", property = "Texture", assetType = "Image", supported = false},
	{className = "Trail", property = "Texture", assetType = "Image", supported = false},
	{className = "Shirt", property = "ShirtTemplate", assetType = "Image", supported = false},
	{className = "Pants", property = "PantsTemplate", assetType = "Image", supported = false},
	{className = "ShirtGraphic", property = "Graphic", assetType = "Image", supported = false},
	{className = "SurfaceAppearance", property = "ColorMap", assetType = "Image", supported = false},
	{className = "SurfaceAppearance", property = "NormalMap", assetType = "Image", supported = false},
	{className = "SurfaceAppearance", property = "MetalnessMap", assetType = "Image", supported = false},
	{className = "SurfaceAppearance", property = "RoughnessMap", assetType = "Image", supported = false},
	{className = "VideoFrame", property = "Video", assetType = "Video", supported = false},
}

-- Group descriptors by ClassName so each Instance checks only the properties
-- relevant to its class, rather than walking the entire registry.
local PROPERTY_REGISTRY_BY_CLASS: {[string]: typeof(PROPERTY_REGISTRY)} = {}
for _, descriptor in ipairs(PROPERTY_REGISTRY) do
	local descriptors = PROPERTY_REGISTRY_BY_CLASS[descriptor.className]
	if not descriptors then
		descriptors = {}
		PROPERTY_REGISTRY_BY_CLASS[descriptor.className] = descriptors
	end
	table.insert(descriptors, descriptor)
end

local EMPTY_PROPERTY_DESCRIPTORS: typeof(PROPERTY_REGISTRY) = {}

local function normalizedAssetId(value: any): string?
	local text = tostring(value or "")
	local id = text:match("^%s*rbxassetid://(%d+)%s*$")
		or text:match("^%s*https?://[^%s]+[?&]id=(%d+)")
		or text:match("^%s*(%d+)%s*$")
	if not id or id == "0" or #id < 5 or #id > 20 then
		return nil
	end
	return id
end

local function scanInstanceProperties(root: Instance, objects: {Instance}?): {AssetManifestEntry}
	local results: {AssetManifestEntry} = {}
	for _, object in ipairs(objects or instancesIncludingRoot(root)) do
		for _, descriptor in ipairs(PROPERTY_REGISTRY_BY_CLASS[object.ClassName] or EMPTY_PROPERTY_DESCRIPTORS) do
			local ok, value = pcall(function()
				return (object :: any)[descriptor.property]
			end)
			if not ok then continue end
			local id = normalizedAssetId(value)
			if not id then continue end
			table.insert(results, {
				name = object.Name .. "." .. descriptor.property,
				path = object:GetFullName() .. " [" .. descriptor.property .. "]",
				relativePath = relativePath(root, object) .. " [" .. descriptor.property .. "]",
				assetId = id,
				assetType = descriptor.assetType,
				referenceKind = "Property",
				propertyName = descriptor.property,
				supported = descriptor.supported,
				note = if descriptor.supported then "" else "Detected only; this asset type is not yet uploadable by TM.",
			})
		end
	end
	return results
end

local function readAssetId(
	object: Instance,
	assetType: string
): string?
	local raw = ""

	if assetType == "Animation" and object:IsA("Animation") then
		raw = (object :: Animation).AnimationId
	elseif assetType == "Audio" and object:IsA("Sound") then
		raw = (object :: Sound).SoundId
	elseif assetType == "Mesh" and object:IsA("MeshPart") then
		local ok, value = pcall(function()
			return (object :: MeshPart).MeshId
		end)
		if not ok then return nil end
		raw = tostring(value)
	elseif assetType == "Mesh" and object:IsA("SpecialMesh") then
		raw = (object :: SpecialMesh).MeshId
	else
		return nil
	end

	return normalizedAssetId(raw)
end

local function writeAssetId(
	object: Instance,
	assetType: string,
	newId: string
): (boolean, string?)
	local ok, err = pcall(function()
		if assetType == "Animation" and object:IsA("Animation") then
			(object :: Animation).AnimationId = "rbxassetid://" .. newId
		elseif assetType == "Audio" and object:IsA("Sound") then
			(object :: Sound).SoundId = "rbxassetid://" .. newId
		elseif assetType == "Mesh" and object:IsA("SpecialMesh") then
			(object :: SpecialMesh).MeshId = "rbxassetid://" .. newId
		elseif assetType == "Mesh" and object:IsA("MeshPart") then
			-- MeshPart.MeshId is read-only. ApplyMesh changes the mesh AND
			-- collision geometry atomically, keeping the original Instance.
			local part = object :: MeshPart
			local oldTexture = part.TextureID
			local replacement = AssetService:CreateMeshPartAsync(
				Content.fromAssetId(tonumber(newId) :: number),
				{
					CollisionFidelity = part.CollisionFidelity,
					RenderFidelity = part.RenderFidelity,
				}
			)
			local applied, applyErr = pcall(function()
				part:ApplyMesh(replacement)
				part.TextureID = oldTexture
			end)
			replacement:Destroy()
			if not applied then error(applyErr) end
		else
			error("Unsupported asset instance")
		end
	end)
	return ok, if ok then nil else tostring(err)
end

-- Scan any Luau source container (Script, LocalScript, ModuleScript).
-- Comment ranges are masked to avoid uploading IDs from disabled code.
local function readScriptSource(object: Instance): string?
	if not object:IsA("LuaSourceContainer") then return nil end
	local ok, value = pcall(function() return (object :: any).Source end)
	return if ok and type(value) == "string" then value else nil
end

local function writeScriptSource(object: Instance, newSource: string): (boolean, string?)
	local ok, reason = pcall(function() (object :: any).Source = newSource end)
	if ok then return true, nil end
	local worked, otherReason = pcall(function()
		ScriptEditorService:UpdateSourceAsync(object :: any, function(_current)
			return newSource
		end)
	end)
	return worked, if worked then nil else tostring(otherReason or reason)
end

local function maskLuaComments(source: string): string
	-- Copy ordinary source in spans. The previous character-at-a-time table
	-- builder created one table entry per byte for large ModuleScripts.
	local parts = {}
	local copyStart = 1
	local i = 1
	local n = #source

	while i <= n do
		local char = source:sub(i, i)
		local nextChar = source:sub(i + 1, i + 1)

		if char == "'" or char == '"' then
			-- Quoted strings are preserved; skip over them so "--" inside a
			-- string literal is never mistaken for a comment.
			i += 1
			while i <= n do
				local current = source:sub(i, i)
				if current == "\\" then
					i += 2
				elseif current == char then
					i += 1
					break
				else
					i += 1
				end
			end
		elseif char == "-" and nextChar == "-" then
			table.insert(parts, source:sub(copyStart, i - 1))
			local begin = i
			local blockLevel = source:sub(i):match("^%-%-%[(=*)%[")
			if blockLevel then
				local closing = "]" .. blockLevel .. "]"
				local afterPrefix = i + 4 + #blockLevel
				local ending = source:find(closing, afterPrefix, true)
				i = if ending then ending + #closing else n + 1
			else
				local newline = source:find("\n", i, true)
				i = newline or (n + 1)
			end

			local fragment = source:sub(begin, i - 1)
			table.insert(parts, (fragment:gsub("[^\n]", " ")))
			copyStart = i
		else
			i += 1
		end
	end

	table.insert(parts, source:sub(copyStart))
	return table.concat(parts)
end

-- Numeric assignments in quoted strings are documentation, not code.
-- Keep the character positions unchanged so exact replacements stay safe.
local function maskQuotedStrings(source: string): string
	-- Preserve byte positions while replacing whole quoted spans at once.
	local parts = {}
	local copyStart = 1
	local i = 1
	local n = #source

	while i <= n do
		local quote = source:sub(i, i)
		if quote == "'" or quote == '"' then
			local begin = i
			i += 1
			while i <= n do
				local current = source:sub(i, i)
				if current == "\\" then
					i += 2
				elseif current == quote then
					i += 1
					break
				else
					i += 1
				end
			end

			table.insert(parts, source:sub(copyStart, begin - 1))
			table.insert(parts, (source:sub(begin, i - 1):gsub("[^\n]", " ")))
			copyStart = i
		else
			i += 1
		end
	end

	table.insert(parts, source:sub(copyStart))
	return table.concat(parts)
end

local function inferScriptAssetType(fieldName: string, insideSounds: boolean): string
	local lower = fieldName:lower()
	if lower:find("mesh", 1, true) or lower == "geometry" then return "Mesh" end
	if lower:find("anim", 1, true) or lower == "emote" then return "Animation" end
	if lower:find("sound", 1, true) or lower:find("music", 1, true)
		or lower:find("audio", 1, true) or lower == "intro" or lower == "outro" or lower == "sfx"
		or lower == "voice" or insideSounds then return "Audio" end
	if lower:find("image", 1, true) or lower:find("icon", 1, true)
		or lower:find("texture", 1, true) or lower:find("decal", 1, true)
		or lower:find("thumbnail", 1, true) or lower:find("preview", 1, true)
		or lower:find("sprite", 1, true) then return "Image" end
	if lower:find("video", 1, true) then return "Video" end
	return "Unknown"
end

-- Only classify an asset as an animation when the *actual call name* makes
-- that inference reliable. Generic methods such as LoadTrack() are deliberately
-- omitted: audio players and custom systems can use those same names.
local ANIMATION_ID_CALLS: {[string]: boolean} = {
	PreloadTrack = true,
	LoadAnimation = true,
	PreloadAnimation = true,
	PlayAnimation = true,
	LoadAnimationTrack = true,
}

local CONTEXT_ANIMATION_KEYS: {[string]: boolean} = {
	idle = true, walk = true, run = true, jump = true, fall = true,
	equip = true, unequip = true, sprint = true, attack = true,
	parry = true, warcry = true, block = true, loop = true,
	drink = true, reload = true, dance = true, swim = true,
	climb = true, hit = true, stunned = true,
}

local function isAnimationActionKey(fieldName: string): boolean
	local lower = fieldName:lower()
	return CONTEXT_ANIMATION_KEYS[lower] == true
		or lower:match('^bite%d+$') ~= nil
		or lower:match('^idle%d+$') ~= nil
		or lower:match('^attack%d+$') ~= nil
		or lower:match('^slash%d+$') ~= nil
		or lower:match('^walk%d+$') ~= nil
		or lower:match('^run%d+$') ~= nil
end

local function scanScriptReferencesRaw(root: Instance, objects: {Instance}?): ({AssetManifestEntry}, {[Instance]: string}, number)
	local results: {AssetManifestEntry} = {}
	local scriptSources: {[Instance]: string} = {}
	local unreadableScripts = 0

	for _, object in ipairs(objects or instancesIncludingRoot(root)) do
		if not object:IsA("LuaSourceContainer") then continue end
		local source = readScriptSource(object)
		if not source then
			unreadableScripts += 1
			continue
		end
		scriptSources[object] = source

		-- Resolve the path lazily: scripts without references need no ancestry walk.
		local fullName: string? = nil
		local objectRelativePath: string? = nil

		-- Line lookup used to scan source[1..offset] for every hit (O(source
		-- length × reference count)). Build the newline index only for scripts
		-- that actually contain a reference, then binary-search it per hit.
		local newlinePositions: {number}? = nil
		local function lineAt(position: number): number
			if not newlinePositions then
				local positions = {}
				for newlinePosition in source:gmatch("()\n") do
					table.insert(positions, newlinePosition)
				end
				newlinePositions = positions
			end

			local positions = newlinePositions :: {number}
			local low, high, count = 1, #positions, 0
			while low <= high do
				local middle = math.floor((low + high) / 2)
				if positions[middle] < position then
					count = middle
					low = middle + 1
				else
					high = middle - 1
				end
			end
			return count + 1
		end

		local clean = maskLuaComments(source)
		local numericClean = maskQuotedStrings(clean)
		-- A common decompiled Animator script holds tables such as
		-- { Idle = 123, Walk = 456 } without using the word Animation in
		-- its table name. We only recognize action keys when the script is
		-- itself an Animator and contains executable animation-loader clues.
		-- AssetURI/id remain ambiguous unless some other reference proves type.
		local animatorContext = object.Name:lower():find("animator", 1, true) ~= nil
			and (numericClean:find("PreloadTrack", 1, true) ~= nil
				or numericClean:find("LoadAnimation", 1, true) ~= nil
				or numericClean:find("PreloadAnimation", 1, true) ~= nil
				or numericClean:find("AnimationTrack", 1, true) ~= nil
				or numericClean:find("AnimationId", 1, true) ~= nil)
		local typedRanges = {}
		-- Infer values inside tables like Animations={Idle=123},
		-- Sounds={Open=123}, Meshes={Head=123}, Icons={Menu=123}.
		-- The name of the table matters; arbitrary tables remain unknown.
		for blockStart, containerName, block in numericClean:gmatch("()([%a_][%w_]*)%s*=%s*(%b{})") do
			local kind = inferScriptAssetType(containerName, false)
			if kind ~= "Unknown" then
				local blockOpen = numericClean:find("{", blockStart, true)
				if blockOpen then
					table.insert(typedRanges, {blockOpen, blockOpen + #block, kind})
				end
			end
		end
		local function typeAt(valueStart: number): string
			local found = "Unknown"
			local narrowest = math.huge
			for _, bounds in ipairs(typedRanges) do
				local width = bounds[2] - bounds[1]
				if valueStart > bounds[1] and valueStart < bounds[2] and width < narrowest then
					found = bounds[3]
					narrowest = width
				end
			end
			return found
		end
		local occupied = {}
		local function addRef(
			fieldName: string,
			id: string,
			valueStart: number,
			valueEnd: number,
			hint: string,
			inferredType: string?
		)
			if id == "0" or #id < 5 or #id > 20 or occupied[valueStart] then return end
			occupied[valueStart] = true
			local line = lineAt(valueStart)
			if not fullName then
				fullName = object:GetFullName()
				objectRelativePath = relativePath(root, object)
			end
			local assetType = inferredType or inferScriptAssetType(fieldName, false)
			if assetType == "Unknown" then assetType = typeAt(valueStart) end
			local contextual = false
			if assetType == "Unknown" and animatorContext and isAnimationActionKey(fieldName) then
				assetType = "Animation"
				contextual = true
			end
			local supported = assetType == "Animation" or assetType == "Audio" or assetType == "Mesh"
			table.insert(results, {
				name = object.Name .. " â¢ " .. fieldName,
				path = (fullName :: string) .. " [" .. fieldName .. ":" .. tostring(line) .. "]",
				relativePath = (objectRelativePath :: string) .. " [" .. fieldName .. ":" .. tostring(line) .. "]",
				assetId = id, assetType = assetType, referenceKind = "Script",
				modulePath = object:GetFullName(), fieldPath = fieldName,
				valueStart = valueStart, valueEnd = valueEnd, supported = supported,
				note = if contextual then "Context inferred: action key in Animator with animation-loader usage; review before migrating"
					elseif supported then "Matched " .. hint
					else "Unclassified/unsupported reference; no automatic replacement",
			})
		end
		-- v3.26: Function-call-aware animation detection. A decompiled call like
		-- u2:PreloadTrack("rbxassetid://73433508214504") has an opaque local
		-- name, so field-only inference would classify its ID as Unknown.
		-- Scan these first so a later generic rbxassetid:// pass cannot claim
		-- the exact same digit range as an Unknown reference.
		--
		-- Use numericClean to verify the method name is executable source,
		-- not documentation in a quoted string; comments are already masked.
		local function validAnimationCall(method: string, methodStart: number): boolean
			if not ANIMATION_ID_CALLS[method] then return false end
			if numericClean:sub(methodStart, methodStart + #method - 1) ~= method then return false end
			local before = numericClean:sub(methodStart - 1, methodStart - 1)
			return methodStart == 1 or not before:match("[%w_]")
		end
		local function closedLiteralArg(quote: string, idEnd: number): boolean
			if clean:sub(idEnd, idEnd) ~= quote then return false end
			-- Full literal first argument only; avoid blindly patching string
			-- concatenations such as "rbxassetid://123" .. suffix.
			return clean:sub(idEnd + 1):match("^%s*[,)]") ~= nil
		end

		-- String form: :PreloadTrack("rbxassetid://12345")
		-- Also works for LoadAnimation(...) and other whitelisted calls.
		for methodStart, method, quote, idStart, id, idEnd in
			clean:gmatch("()([%a_][%w_]*)%s*%(%s*(['\"])%s*rbxassetid://()(%d+)()") do
			if validAnimationCall(method, methodStart) and closedLiteralArg(quote, idEnd) then
				addRef(method, id, idStart, idEnd, "animation loader " .. method .. "()", "Animation")
			end
		end

		-- String/numeric form: :PreloadTrack("12345") or :PreloadTrack(12345)
		for methodStart, method, quote, idStart, id, idEnd in
			clean:gmatch("()([%a_][%w_]*)%s*%(%s*(['\"])()(%d+)()") do
			if validAnimationCall(method, methodStart) and closedLiteralArg(quote, idEnd) then
				addRef(method, id, idStart, idEnd, "animation loader " .. method .. "()", "Animation")
			end
		end
		for methodStart, method, idStart, id, idEnd in
			numericClean:gmatch("()([%a_][%w_]*)%s*%(%s*()(%d+)()") do
			if validAnimationCall(method, methodStart)
				and numericClean:sub(idEnd):match("^%s*[,)]") ~= nil then
				addRef(method, id, idStart, idEnd, "animation loader " .. method .. "()", "Animation")
			end
		end

		-- Typed and arbitrary numeric fields. Unknown fields are *preview only*.
		for _fieldStart, field, numberStart, id, numberEnd in numericClean:gmatch("()([%a_][%w_]*)%s*=%s*()(%d+)()") do
			if not (#id < 5 or #id > 20) then
				local kind = inferScriptAssetType(field, false)
				if kind == "Unknown" then kind = typeAt(numberStart) end
				if kind ~= "Unknown" or field:lower():find("id", 1, true)
					or field:lower():find("asset", 1, true)
					or (animatorContext and isAnimationActionKey(field)) then
					addRef(field, id, numberStart, numberEnd, "numeric field")
				end
			end
		end
		-- Bracketed table keys, e.g. Animations = { ["Idle"] = 12345 }.
		for field, numberStart, id, numberEnd in clean:gmatch("%[%s*['\"]([%a_][%w_]*)['\"]%s*%]%s*=%s*()(%d+)()") do
			if typeAt(numberStart) ~= "Unknown" or inferScriptAssetType(field, false) ~= "Unknown"
				or (animatorContext and isAnimationActionKey(field)) then
				addRef(field, id, numberStart, numberEnd, "bracketed table key")
			end
		end
		-- Literal rbxassetid:// values; keep the prefix and replace just digits.
		for prefixStart, id, numberEnd in clean:gmatch("()rbxassetid://(%d+)()") do
			local valueStart = prefixStart + #"rbxassetid://"
			local before = clean:sub(math.max(1, prefixStart - 180), prefixStart - 1)
			local field = before:match("([%a_][%w_]*)%s*=%s*['\"]%s*$")
				or before:match("([%a_][%w_]*)%s*=%s*$")
				or "AssetURI"
			addRef(field, id, valueStart, numberEnd, "rbxassetid URI")
		end
	end
	return results, scriptSources, unreadableScripts
end

-- v3.27: Two-pass, selection-wide type resolution. It is crucial to
-- consider native instance properties too; an ID can appear once as a real
-- Animation.AnimationId and elsewhere as a generic AssetURI.
-- Conversely, the same ID tagged Audio and Animation is NOT safe to guess.
local function scanAllReferences(root: Instance, objects: {Instance}?): ({AssetManifestEntry}, {[Instance]: string}, number)
	local objectList = objects or instancesIncludingRoot(root)
	local entries = scanInstanceProperties(root, objectList)
	local scriptEntries, scriptSources, unreadableScripts = scanScriptReferencesRaw(root, objectList)
	for _, item in ipairs(scriptEntries) do
		table.insert(entries, item)
	end

	local evidence: {[string]: {[string]: boolean}} = {}
	for _, item in ipairs(entries) do
		if item.assetType ~= "Unknown" then
			if not evidence[item.assetId] then evidence[item.assetId] = {} end
			evidence[item.assetId][item.assetType] = true
		end
	end

	for _, item in ipairs(entries) do
		local kinds = evidence[item.assetId]
		if not kinds then continue end
		local list = {}
		for kind in pairs(kinds) do table.insert(list, kind) end
		table.sort(list)
		if #list > 1 then
			-- Also quarantine the confidently typed property/script entry. This
			-- prevents type collisions from triggering a wrong asset upload.
			item.assetType = "Unknown"
			item.supported = false
			item.note = "TYPE CONFLICT (" .. table.concat(list, " vs ") .. "); no patch allowed"
		elseif item.assetType == "Unknown" then
			item.assetType = list[1]
			item.supported = item.assetType == "Animation" or item.assetType == "Audio" or item.assetType == "Mesh"
			item.note = "Cross-referenced matching asset ID; "
				.. (if item.supported then "type confirmed by another reference" else "scan only")
		end
	end

	return entries, scriptSources, unreadableScripts
end

local function makeManifest(root: Instance, assetType: string, preScannedEntries: {AssetManifestEntry}?): Manifest
	local assets: {AssetManifestEntry} = {}
	local entries = preScannedEntries or scanAllReferences(root)
	for _, entry in ipairs(entries) do
		if entry.assetType == assetType and entry.supported then
			table.insert(assets, entry)
		end
	end
	return {tower = root.Name, rootPath = root:GetFullName(), assetType = assetType, assets = assets}
end

local function uniqueIds(manifest: Manifest): {[string]: boolean}
	local ids = {}

	for _, entry in ipairs(manifest.assets) do
		ids[entry.assetId] = true
	end

	return ids
end

local function countDictionary(dictionary: {[any]: any}): number
	local count = 0

	for _ in pairs(dictionary) do
		count += 1
	end

	return count
end

local function currentSelection(): Instance?
	local selected = Selection:Get()

	if #selected ~= 1 then
		return nil
	end

	return selected[1]
end

local function cachedCount(assetType: string, ids: {[string]: boolean}): number
	local cached = 0

	for id in pairs(ids) do
		if cachedNewId(assetType, id) then
			cached += 1
		end
	end

	return cached
end

local function updateSelectionPreview()
	local root = currentSelection()
	if not root then
		selectionLabel.Text = "Selection: select exactly one folder/model/asset."
		return
	end
	local references = scanAllReferences(root, instancesIncludingRoot(root))
	local counts: {[string]: number} = {}
	local ids: {[string]: {[string]: boolean}} = {}
	for _, entry in ipairs(references) do
		if not entry.supported then continue end
		counts[entry.assetType] = (counts[entry.assetType] or 0) + 1
		if not ids[entry.assetType] then ids[entry.assetType] = {} end
		ids[entry.assetType][entry.assetId] = true
	end
	local animationIds = ids.Animation or {}
	local audioIds = ids.Audio or {}
	local meshIds = ids.Mesh or {}
	selectionLabel.Text = (
		"Selection: %s\nAnims: %d/%d (%d cached)  â¢  Sounds: %d/%d (%d cached)  â¢  Meshes: %d/%d (%d cached)"
	):format(
		root:GetFullName(),
		counts.Animation or 0,
		countDictionary(animationIds),
		cachedCount("Animation", animationIds),
		counts.Audio or 0,
		countDictionary(audioIds),
		cachedCount("Audio", audioIds),
		counts.Mesh or 0,
		countDictionary(meshIds),
		cachedCount("Mesh", meshIds)
	)
end

----------------------------------------------------------------
-- Dry run / universal asset inventory (NO HTTP, NO UPLOADS, NO EDITS)
----------------------------------------------------------------

local previewRoot: Instance? = nil
local previewSignature: string? = nil

local function previewFingerprint(
	root: Instance,
	objects: {Instance}?,
	preScannedEntries: {AssetManifestEntry}?,
	preScannedSources: {[Instance]: string}?
): string
	-- Compact digest of selected scripts + scanned references and settings.
	-- Recomputed before migration: stale previews are never auto-approved.
	local objectList = objects or instancesIncludingRoot(root)
	local entries = preScannedEntries
	local scriptSources = preScannedSources
	if not entries or not scriptSources then
		local scannedEntries, scannedSources = scanAllReferences(root, objectList)
		entries = entries or scannedEntries
		scriptSources = scriptSources or scannedSources
	end
	local fingerprintEntries: {AssetManifestEntry} = entries or {}
	local fingerprintSources: {[Instance]: string} = scriptSources or {}
	local hash = 5381
	local function append(text: string)
		for i = 1, #text do
			hash = (hash * 33 + string.byte(text, i)) % 4294967296
		end
	end
	append(tostring(forceReupload))
	local cacheKeys = {}
	for key in pairs(mappingCache) do table.insert(cacheKeys, key) end
	table.sort(cacheKeys)
	for _, key in ipairs(cacheKeys) do append(key .. "=" .. tostring(mappingCache[key])) end
	for _, assetType in ipairs(ASSET_TYPE_ORDER) do
		append(assetType .. tostring(enabledAssetTypes[assetType]))
	end
	for _, entry in ipairs(fingerprintEntries) do
		append(entry.path .. ":" .. entry.assetType .. ":" .. entry.assetId)
	end
	for _, object in ipairs(objectList) do
		if object:IsA("LuaSourceContainer") then
			append(object:GetFullName())
			append(fingerprintSources[object] or "[Source inaccessible]")
		end
	end
	return tostring(hash)
end

local function previewSelected(): boolean
	if busy then return false end
	if RunService:IsRunning() then
		setStatus("Stop playtest first", "Dry runs are intended for Studio edit mode.", true)
		return false
	end
	local root = currentSelection()
	if not root then
		setStatus("Select one item", "Choose exactly one folder, model, instance, or script.", true)
		return false
	end
	local objects = instancesIncludingRoot(root)
	local allEntries, scriptSources, unreadableScripts = scanAllReferences(root, objects)
	local byType: {[string]: {[string]: boolean}} = {}
	local supportedSelected: {[string]: {[string]: boolean}} = {}
	local cachedByType: {[string]: {[string]: boolean}} = {}
	local referenceCounts: {[string]: number} = {}
	local selectedReferences = 0
	local unsupportedReferences = 0
	local conflictReferences = 0
	local crossReferenced = 0
	local contextualReferences = 0
	local lines = {
		"RbxMigrator â CONTEXT-AWARE UNIVERSAL SCAN / DRY RUN",
		"=======================================",
		"Selection: " .. root:GetFullName(),
		"Mode: READ-ONLY. No uploads, downloads, source edits, or ID changes.",
		"Force reupload: " .. tostring(forceReupload),
		"",
	}
	for _, entry in ipairs(allEntries) do
		if entry.note and entry.note:find("TYPE CONFLICT", 1, true) then conflictReferences += 1 end
		if entry.note and entry.note:find("Cross-referenced", 1, true) then crossReferenced += 1 end
		if entry.note and entry.note:find("Context inferred", 1, true) then contextualReferences += 1 end
		local kind = entry.assetType
		if not byType[kind] then byType[kind] = {} end
		byType[kind][entry.assetId] = true
		referenceCounts[kind] = (referenceCounts[kind] or 0) + 1
		if entry.supported and enabledAssetTypes[kind] == true then
			selectedReferences += 1
			if not supportedSelected[kind] then supportedSelected[kind] = {} end
			supportedSelected[kind][entry.assetId] = true
			if not forceReupload and cachedNewId(kind, entry.assetId) then
				if not cachedByType[kind] then cachedByType[kind] = {} end
				cachedByType[kind][entry.assetId] = true
			end
		elseif not entry.supported then
			unsupportedReferences += 1
		end
	end
	local uniqueToUpload = 0
	local uniqueCached = 0
	table.insert(lines, "INVENTORY (references / unique IDs)")
	for _, kind in ipairs({"Animation", "Audio", "Mesh", "Image", "Video", "Unknown"}) do
		local ids = byType[kind] or {}
		table.insert(lines, ("%-10s %5d / %5d %s"):format(kind, referenceCounts[kind] or 0, countDictionary(ids),
			if enabledAssetTypes[kind] then "[enabled]" elseif kind == "Image" or kind == "Video" or kind == "Unknown" then "[scan only]" else "[disabled]"))
		local selected = supportedSelected[kind] or {}
		local cached = cachedByType[kind] or {}
		uniqueCached += countDictionary(cached)
		uniqueToUpload += countDictionary(selected) - countDictionary(cached)
	end
	table.insert(lines, "")
	table.insert(lines, ("Selected patchable references: %d"):format(selectedReferences))
	table.insert(lines, ("Unique IDs needing upload: %d"):format(uniqueToUpload))
	table.insert(lines, ("Unique IDs with cache mapping: %d"):format(uniqueCached))
	table.insert(lines, ("Unsupported/ambiguous references: %d"):format(unsupportedReferences))
	table.insert(lines, ("Resolved by matching IDs: %d"):format(crossReferenced))
	table.insert(lines, ("Resolved by Animator context: %d"):format(contextualReferences))
	table.insert(lines, ("Type conflict references (blocked): %d"):format(conflictReferences))
	table.insert(lines, ("Unreadable scripts (not scanned): %d"):format(unreadableScripts))
	table.insert(lines, "")
	table.insert(lines, "DETAILED PLAN â ID | Type | Action | Where")
	table.sort(allEntries, function(a, b)
		if a.assetType ~= b.assetType then return a.assetType < b.assetType end
		return a.relativePath < b.relativePath
	end)
	for _, entry in ipairs(allEntries) do
		local action
		if not entry.supported then
			action = "SCAN ONLY"
		elseif not enabledAssetTypes[entry.assetType] then
			action = "DISABLED"
		elseif not forceReupload and cachedNewId(entry.assetType, entry.assetId) then
			action = "CACHE -> " .. tostring(cachedNewId(entry.assetType, entry.assetId))
		else
			action = "UPLOAD + PATCH"
		end
		local explanation = entry.note or ""
		if explanation ~= "" then explanation = " | " .. explanation end
		table.insert(lines, ("%s | %s | %s | %s%s"):format(entry.assetId, entry.assetType, action, entry.relativePath, explanation))
	end
	table.insert(lines, "")
	table.insert(lines, "WARNING: Counts are estimates only. Permission issues, moderation, and Roblox API errors can prevent uploads or patches.")
	table.insert(lines, "Images, video and unknown IDs are discoverable but NOT uploadable in this release.")
	table.insert(lines, "Lua scripts using dynamic/constructed IDs cannot be reliably classified automatically.")
	table.insert(lines, "Type conflicts are quarantined across the entire selection, including native properties.")
	table.insert(lines, "Animator context is a heuristic. Check those lines before approving migration.")
	table.insert(lines, "If the plan looks right, click Migrate Selected to proceed.")
	setReport(table.concat(lines, "\n"))
	previewRoot = root
	previewSignature = previewFingerprint(root, objects, allEntries, scriptSources)
	setStatus("Dry run ready â nothing changed",
		("%d references found â¢ %d uploads planned â¢ %d cached â¢ %d scan-only/uncertain. Review the report before migrating."):format(
			#allEntries, uniqueToUpload, uniqueCached, unsupportedReferences))
	return true
end

----------------------------------------------------------------
-- Official Studio source serialization
----------------------------------------------------------------

local function xmlEscape(value: any): string
	local text = tostring(value)

	text = text:gsub("&", "&amp;")
	text = text:gsub("<", "&lt;")
	text = text:gsub(">", "&gt;")
	text = text:gsub('"', "&quot;")
	text = text:gsub("'", "&apos;")

	return text
end

local nextReferent = 0

local function newReferent(): string
	nextReferent += 1
	return "RBX" .. tostring(nextReferent)
end

local function propertyString(name: string, value: any): string
	return '<string name="'
		.. xmlEscape(name)
		.. '">'
		.. xmlEscape(value)
		.. "</string>"
end

local function propertyBool(name: string, value: boolean): string
	return '<bool name="'
		.. xmlEscape(name)
		.. '">'
		.. (if value then "true" else "false")
		.. "</bool>"
end

local function propertyFloat(name: string, value: number): string
	return '<float name="'
		.. xmlEscape(name)
		.. '">'
		.. string.format("%.17g", value)
		.. "</float>"
end

local function propertyToken(name: string, value: number): string
	return '<token name="'
		.. xmlEscape(name)
		.. '">'
		.. tostring(value)
		.. "</token>"
end

local function propertyCFrame(name: string, value: CFrame): string
	local x, y, z,
		r00, r01, r02,
		r10, r11, r12,
		r20, r21, r22 = value:GetComponents()

	return '<CoordinateFrame name="' .. xmlEscape(name) .. '">'
		.. "<X>" .. string.format("%.17g", x) .. "</X>"
		.. "<Y>" .. string.format("%.17g", y) .. "</Y>"
		.. "<Z>" .. string.format("%.17g", z) .. "</Z>"
		.. "<R00>" .. string.format("%.17g", r00) .. "</R00>"
		.. "<R01>" .. string.format("%.17g", r01) .. "</R01>"
		.. "<R02>" .. string.format("%.17g", r02) .. "</R02>"
		.. "<R10>" .. string.format("%.17g", r10) .. "</R10>"
		.. "<R11>" .. string.format("%.17g", r11) .. "</R11>"
		.. "<R12>" .. string.format("%.17g", r12) .. "</R12>"
		.. "<R20>" .. string.format("%.17g", r20) .. "</R20>"
		.. "<R21>" .. string.format("%.17g", r21) .. "</R21>"
		.. "<R22>" .. string.format("%.17g", r22) .. "</R22>"
		.. "</CoordinateFrame>"
end

local function commonProperties(object: Instance): string
	return propertyString("Name", object.Name)
end

local serializePose: (Pose) -> string

serializePose = function(pose: Pose): string
	local children = {}

	for _, child in ipairs(pose:GetChildren()) do
		if child:IsA("Pose") then
			table.insert(children, serializePose(child))
		end
	end

	return '<Item class="Pose" referent="' .. newReferent() .. '"><Properties>'
		.. propertyCFrame("CFrame", pose.CFrame)
		.. propertyToken("EasingDirection", pose.EasingDirection.Value)
		.. propertyToken("EasingStyle", pose.EasingStyle.Value)
		.. propertyFloat("Weight", pose.Weight)
		.. commonProperties(pose)
		.. "</Properties>"
		.. table.concat(children)
		.. "</Item>"
end

local function serializeMarker(marker: KeyframeMarker): string
	return '<Item class="KeyframeMarker" referent="' .. newReferent() .. '"><Properties>'
		.. propertyString("Value", marker.Value)
		.. commonProperties(marker)
		.. "</Properties></Item>"
end

local function serializeKeyframe(keyframe: Keyframe): string
	local children = {}

	for _, child in ipairs(keyframe:GetChildren()) do
		if child:IsA("Pose") then
			table.insert(children, serializePose(child))
		elseif child:IsA("KeyframeMarker") then
			table.insert(children, serializeMarker(child))
		end
	end

	return '<Item class="Keyframe" referent="' .. newReferent() .. '"><Properties>'
		.. propertyFloat("Time", keyframe.Time)
		.. commonProperties(keyframe)
		.. "</Properties>"
		.. table.concat(children)
		.. "</Item>"
end

local function serializeKeyframeSequence(sequence: KeyframeSequence): string
	nextReferent = 0

	local children = {}

	for _, keyframe in ipairs(sequence:GetKeyframes()) do
		table.insert(children, serializeKeyframe(keyframe))
	end

	local authoredHipHeight = 0

	pcall(function()
		authoredHipHeight = sequence.AuthoredHipHeight
	end)

	return '<?xml version="1.0" encoding="utf-8"?>'
		.. '<roblox xmlns:xmime="http://www.w3.org/2005/05/xmlmime" '
		.. 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" version="4">'
		.. "<External>null</External><External>nil</External>"
		.. '<Item class="KeyframeSequence" referent="' .. newReferent() .. '"><Properties>'
		.. propertyFloat("AuthoredHipHeight", authoredHipHeight)
		.. propertyBool("Loop", sequence.Loop)
		.. propertyToken("Priority", sequence.Priority.Value)
		.. commonProperties(sequence)
		.. "</Properties>"
		.. table.concat(children)
		.. "</Item></roblox>"
end

local function prepareOfficialStudioSource(
	assetId: string
): (boolean, string?, string?)
	local clip: AnimationClip? = nil

	local ok, failure = pcall(function()
		clip = AnimationClipProvider:GetAnimationClipAsync(
			"rbxassetid://" .. assetId
		)
	end)

	if not ok or not clip then
		return false, tostring(failure), nil
	end

	if not clip:IsA("KeyframeSequence") then
		local className = clip.ClassName
		clip:Destroy()

		return false,
			"Studio loaded "
			.. className
			.. "; direct serialization currently supports KeyframeSequence.",
			nil
	end

	local rbxmx: string? = nil

	local serializeOk, serializeFailure = pcall(function()
		rbxmx = serializeKeyframeSequence(
			clip :: KeyframeSequence
		)
	end)

	clip:Destroy()

	if not serializeOk or not rbxmx then
		return false, tostring(serializeFailure), nil
	end

	return true, nil, rbxmx
end

local function reportStudioSourceBatch(
	jobId: string,
	items: {any}
)
	if #items == 0 then
		return
	end

	request(
		"POST",
		"/sources/batch",
		{
			jobId = jobId,
			items = items,
		}
	)
end

----------------------------------------------------------------
-- Replacement / undo
----------------------------------------------------------------

local function applyMapping(root: Instance, assetType: string, mapping: Mapping): (number, {[string]: number})
	local replaced = 0
	local counts: {[string]: number} = {}
	local function record(oldId: string)
		counts[oldId] = (counts[oldId] or 0) + 1
		replaced += 1
	end
	pcall(function() ChangeHistoryService:SetWaypoint("Before Tower Asset Migration - " .. assetType) end)
	local objects = instancesIncludingRoot(root)
	local safeNativeIds: {[string]: boolean} = {}
	local resolvedReferences = scanAllReferences(root, objects)
	for _, item in ipairs(resolvedReferences) do
		if item.assetType == assetType and item.supported then
			safeNativeIds[item.assetId] = true
		end
	end
	for _, object in ipairs(objects) do
		local oldId = readAssetId(object, assetType)
		local newId = if oldId and safeNativeIds[oldId] then mapping[oldId] else nil
		if newId and newId ~= oldId then
			local ok, err = writeAssetId(object, assetType, newId)
			if ok then record(oldId :: string)
			else warn("[TM] Failed to apply " .. assetType .. " on " .. object:GetFullName() .. ": " .. tostring(err)) end
		end
	end
	local byScript: {[Instance]: {AssetManifestEntry}} = {}
	local scriptsByPath: {[string]: Instance} = {}
	for _, object in ipairs(objects) do
		if object:IsA("LuaSourceContainer") then scriptsByPath[object:GetFullName()] = object end
	end
	for _, reference in ipairs(resolvedReferences) do
		if reference.referenceKind ~= "Script" or reference.assetType ~= assetType or not reference.supported then continue end
		-- Use instance identity, never instance path/name, for edits.
		-- A script may hold many asset references with different asset types.
		local object = scriptsByPath[reference.modulePath or ""]
		if object then
			if not byScript[object] then byScript[object] = {} end
			table.insert(byScript[object], reference)
		end
	end
	for object, references in pairs(byScript) do
		local original = readScriptSource(object)
		if not original then continue end
		table.sort(references, function(a, b)
			return (a.valueStart or 0) > (b.valueStart or 0)
		end)
		local edited = original
		local localCounts: {[string]: number} = {}
		for _, reference in ipairs(references) do
			local oldId = reference.assetId
			local replacement = mapping[oldId]
			local startAt, endAt = reference.valueStart, reference.valueEnd
			if not replacement or replacement == oldId or not startAt or not endAt then continue end
			-- Refuse a stale offset rather than editing unrelated source text.
			if original:sub(startAt, endAt - 1) ~= oldId then continue end
			edited = edited:sub(1, startAt - 1) .. replacement .. edited:sub(endAt)
			localCounts[oldId] = (localCounts[oldId] or 0) + 1
		end
		if edited ~= original then
			local ok, err = writeScriptSource(object, edited)
			if ok then
				for oldId, count in pairs(localCounts) do
					for _ = 1, count do record(oldId) end
				end
			else warn("[TM] Failed to patch script " .. object:GetFullName() .. ": " .. tostring(err)) end
		end
	end
	pcall(function() ChangeHistoryService:SetWaypoint("After Tower Asset Migration - " .. assetType) end)
	return replaced, counts
end

----------------------------------------------------------------
-- Reports
----------------------------------------------------------------

local function buildReport(
	root: Instance,
	manifest: Manifest,
	cachedUsed: number,
	newMapping: Mapping,
	errors: {[string]: any},
	replaced: number,
	counts: {[string]: number}
): string
	local label = ASSET_TYPE_LABELS[manifest.assetType] or manifest.assetType

	local lines = {
		"TOWER ASSET MIGRATION â " .. string.upper(label),
		"========================================",
		"Selection: " .. root:GetFullName(),
		"Asset type: " .. manifest.assetType,
		"Instances referencing assets: " .. tostring(#manifest.assets),
		"Unique source assets: " .. tostring(countDictionary(uniqueIds(manifest))),
		"Cached mappings reused: " .. tostring(cachedUsed),
		"New mappings created: " .. tostring(countDictionary(newMapping)),
		"Instances replaced: " .. tostring(replaced),
		"Failed unique assets: " .. tostring(countDictionary(errors)),
		"",
		"OLD -> NEW",
		"----------",
	}

	local combined: Mapping = {}

	for oldId, newId in pairs(mappingCache) do
		if counts[oldId] then
			combined[oldId] = newId
		end
	end

	for oldId, newId in pairs(newMapping) do
		combined[oldId] = newId
	end

	local ids = {}

	for oldId in pairs(combined) do
		table.insert(ids, oldId)
	end

	table.sort(ids)

	for _, oldId in ipairs(ids) do
		table.insert(
			lines,
			("%s -> %s | %d instance(s)"):format(
				oldId,
				combined[oldId],
				counts[oldId] or 0
			)
		)
	end

	if next(errors) ~= nil then
		table.insert(lines, "")
		table.insert(lines, "FAILURES")
		table.insert(lines, "--------")

		local errorIds = {}

		for id in pairs(errors) do
			table.insert(errorIds, id)
		end

		table.sort(errorIds)

		for _, id in ipairs(errorIds) do
			table.insert(
				lines,
				id .. ": " .. tostring(errors[id])
			)
		end
	end

	return table.concat(lines, "\n")
end

----------------------------------------------------------------
-- Migration
----------------------------------------------------------------

local busy = false

local function setBusy(value: boolean)
	busy = value

	-- Run the progress-bar shimmer only while work is in flight.
	if value then
		progressShimmer:Play()
	else
		progressShimmer:Cancel()
		progressGradient.Offset = Vector2.new(-1, 0)
	end

	-- Only actions inside the window are disabled while a migration runs.
	-- The toolbar button is just a window toggle, so it stays usable.
	migrateUiButton.Active = not value
	previewUiButton.Active = not value
	applyCacheButton.Active = not value
	clearCacheButton.Active = not value
	animationTypeButton.Active = not value
	soundTypeButton.Active = not value
	meshTypeButton.Active = not value
end


local function migrateAssetType(
	root: Instance,
	assetType: string,
	progressStart: number,
	progressSpan: number,
	retryAttempt: number?,
	preScannedEntries: {AssetManifestEntry}?
): (string, number, number, number)
	local manifest = makeManifest(root, assetType, preScannedEntries)
	local label = ASSET_TYPE_LABELS[assetType] or assetType

	if #manifest.assets == 0 then
		return ("%s\n%s\nNo %s were found under %s."):format(
			string.upper(label),
			string.rep("=", #label),
			string.lower(label),
			root:GetFullName()
		), 0, 0, 0
	end

	local function typeProgress(localValue: number)
		setProgress(progressStart + progressSpan * math.clamp(localValue, 0, 1))
	end

	local ids = uniqueIds(manifest)
	local cachedMapping: Mapping = {}
	local unresolved: {[string]: boolean} = {}

	for id in pairs(ids) do
		-- The initial Force Re-upload pass ignores the cache, but retry passes must
		-- reuse successful mappings and only submit IDs that still need migration.
		local canReuseCache = not forceReupload or (retryAttempt or 0) > 0
		local cachedId = if canReuseCache then cachedNewId(assetType, id) else nil
		if cachedId then
			cachedMapping[id] = cachedId
		else
			unresolved[id] = true
		end
	end

	local cachedUsed = countDictionary(cachedMapping)

	if next(unresolved) == nil then
		setStatus(
			"Using cached " .. label,
			"Every unique source already has a cached replacement."
		)

		local replaced, counts = applyMapping(root, assetType, cachedMapping)
		typeProgress(1)

		return buildReport(
			root,
			manifest,
			cachedUsed,
			{},
			{},
			replaced,
			counts
		), replaced, 0, 0
	end

	if not checkHelper(false) then
		error(
			"Desktop helper is offline or not configured. "
				.. "Start RbxMigrator.py first."
		)
	end

	if assetType == "Audio" then
		setStatus(
			"Preparing Sounds",
			"Only migrate audio you own or have permission to use. Roblox audio upload limits apply."
		)
	end

	local unresolvedCount = countDictionary(unresolved)
	local unresolvedAssets = {}

	for _, entry in ipairs(manifest.assets) do
		if unresolved[entry.assetId] then
			table.insert(unresolvedAssets, entry)
		end
	end

	local requestManifest = {
		tower = manifest.tower .. " â¢ " .. label,
		rootPath = manifest.rootPath,
		assetType = assetType,
		assets = unresolvedAssets,
		-- Backward compatibility for older helpers when migrating animations.
		animations = if assetType == "Animation" then unresolvedAssets else nil,
		forceReupload = forceReupload,
	}

	local preparedJob = request(
		"POST",
		"/jobs/prepare",
		requestManifest
	)

	local jobId = preparedJob.jobId

	if type(jobId) ~= "string" or jobId == "" then
		error("Desktop helper did not return a preparation job ID.")
	end

	if assetType == "Animation" then
		local preparationIds = {}

		for id in pairs(unresolved) do
			table.insert(preparationIds, id)
		end

		table.sort(preparationIds)

		local preparationTotal = #preparationIds
		local workerCount = math.min(
			STUDIO_PREP_MAX_WORKERS,
			preparationTotal
		)

		local nextPreparationIndex = 1
		local preparationFinished = 0
		local reportedFinished = 0
		local studioSourceSuccesses = 0
		local studioSourceFallbacks = 0
		local reportQueue = {}
		local reporterFailure: string? = nil

		setStatus(
			"Preparing Animations in parallel",
			("%d animation(s) â¢ %d source workers â¢ batched reporting"):format(
				preparationTotal,
				workerCount
			)
		)

		local function preparationWorker()
			while true do
				local index = nextPreparationIndex
				nextPreparationIndex += 1

				local id = preparationIds[index]

				if not id then
					break
				end

				local sourceOk, sourceError, rbxmx = prepareOfficialStudioSource(id)

				if sourceOk then
					studioSourceSuccesses += 1
				else
					studioSourceFallbacks += 1
				end

				table.insert(reportQueue, {
					assetId = id,
					success = sourceOk,
					rbxmx = rbxmx,
					error = sourceError or "",
				})

				preparationFinished += 1

				typeProgress(
					0.18 * (
						preparationFinished / math.max(preparationTotal, 1)
					)
				)

				setStatus(
					"Preparing Animations in parallel",
					("%d/%d prepared â¢ %d direct â¢ %d fallback â¢ %d workers"):format(
						preparationFinished,
						preparationTotal,
						studioSourceSuccesses,
						studioSourceFallbacks,
						workerCount
					)
				)
			end
		end

		local function reporterWorker()
			while reportedFinished < preparationTotal do
				if #reportQueue == 0 then
					task.wait(0.03)
					continue
				end

				local batch = {}

				while #batch < STUDIO_REPORT_BATCH_SIZE and #reportQueue > 0 do
					table.insert(batch, table.remove(reportQueue, 1))
				end

				local reportOk, reportError = pcall(function()
					reportStudioSourceBatch(jobId, batch)
				end)

				if not reportOk then
					reporterFailure = tostring(reportError)
					return
				end

				reportedFinished += #batch
				task.wait(0.03)
			end
		end

		for _ = 1, workerCount do
			task.spawn(preparationWorker)
		end

		task.spawn(reporterWorker)

		while preparationFinished < preparationTotal or reportedFinished < preparationTotal do
			if reporterFailure then
				error(
					"Could not report Studio preparation to the helper: "
						.. reporterFailure
				)
			end

			task.wait(0.05)
		end
	else
		-- Sounds do not need an AnimationClipProvider stage.
		typeProgress(0.18)
	end

	setStatus(
		"Starting " .. label .. " migration",
		("Starting %d unique %s..."):format(
			unresolvedCount,
			string.lower(label)
		)
	)

	request(
		"POST",
		"/jobs/" .. jobId .. "/start",
		{}
	)

	local startedAt = os.clock()
	local job: any = nil

	while true do
		if os.clock() - startedAt > MAX_WAIT_SECONDS then
			error(label .. " migration timed out.")
		end

		task.wait(POLL_INTERVAL)
		job = request("GET", "/jobs/" .. jobId)

		local completed = tonumber(job.completed) or 0
		local total = math.max(tonumber(job.total) or unresolvedCount, 1)
		local overallProgress = tonumber(job.overallProgress)

		if overallProgress then
			typeProgress(math.clamp(overallProgress, 0, 1))
		else
			typeProgress(0.18 + 0.74 * math.clamp(completed / total, 0, 1))
		end

		setStatus(
			("%s %d/%d"):format(label, completed, total),
			tostring(job.message or job.state or "Working...")
		)

		if job.state == "done" or job.state == "partial" or job.state == "failed" then
			break
		end
	end

	local newMapping: Mapping = {}

	if type(job.mapping) == "table" then
		for oldId, newId in pairs(job.mapping) do
			if type(oldId) == "string"
				and type(newId) == "string"
				and oldId:match("^%d+$")
				and newId:match("^%d+$")
			then
				newMapping[oldId] = newId
				mappingCache[cacheKey(assetType, oldId)] = newId
			end
		end
	end

	saveCache(mappingCache)

	local errors = {}

	if type(job.errors) == "table" then
		for id, err in pairs(job.errors) do
			errors[tostring(id)] = err
		end
	end

	local combined: Mapping = {}

	for oldId, newId in pairs(cachedMapping) do
		combined[oldId] = newId
	end

	for oldId, newId in pairs(newMapping) do
		combined[oldId] = newId
	end

	local replaced, counts = applyMapping(root, assetType, combined)
	typeProgress(1)

	return buildReport(
		root,
		manifest,
		cachedUsed,
		newMapping,
		errors,
		replaced,
		counts
	), replaced, countDictionary(newMapping), countDictionary(errors)
end

local function migrateSelected()
	if busy then
		setStatus(
			"Already migrating",
			"Wait for the current migration to finish."
		)
		return
	end

	if RunService:IsRunning() then
		setStatus(
			"Stop the playtest first",
			"Run migrations in Studio edit mode.",
			true
		)
		return
	end

	local root = currentSelection()

	if not root then
		setStatus(
			"Select one item",
			"Select exactly one folder/model/Animation/Sound/MeshPart/SpecialMesh/ModuleScript.",
			true
		)
		return
	end

	-- Mandatory explicit preview: Migration cannot begin using an outdated
	-- selection, checkbox set, cache setting, or script/property inventory.
	local scanObjects = instancesIncludingRoot(root)
	local scanEntries, scanSources = scanAllReferences(root, scanObjects)
	local fingerprint = previewFingerprint(root, scanObjects, scanEntries, scanSources)
	if previewRoot ~= root or previewSignature ~= fingerprint then
		previewSelected()
		setStatus("Review dry run before migrating", "Preview updated. Read the plan, then click Migrate Selected again to confirm.")
		return
	end
	previewSignature = nil -- approval is single-use

	local selectedTypes = {}

	for _, assetType in ipairs(ASSET_TYPE_ORDER) do
		if enabledAssetTypes[assetType] then
			table.insert(selectedTypes, assetType)
		end
	end

	if #selectedTypes == 0 then
		setStatus(
			"Choose an asset type",
			"Enable Animations, Sounds, Meshes, or any combination.",
			true
		)
		return
	end

	local enabledTypes: {[string]: boolean} = {}
	for _, assetType in ipairs(selectedTypes) do
		enabledTypes[assetType] = true
	end
	local anyAssets = false
	for _, entry in ipairs(scanEntries) do
		if entry.supported and enabledTypes[entry.assetType] then
			anyAssets = true
			break
		end
	end

	if not anyAssets then
		setStatus(
			"No matching assets found",
			"The selection does not contain any enabled asset types.",
			true
		)
		return
	end

	setBusy(true)
	setProgress(0)
	widget.Enabled = true

	task.spawn(function()
		local success, failure = pcall(function()
			local reports = {}
			local totalReplaced = 0
			local totalUploaded = 0
			local totalFailed = 0
			local totalRetries = 0
			local span = 1 / #selectedTypes

			for index, assetType in ipairs(selectedTypes) do
				local progressStart = (index - 1) * span
				local report, replaced, uploaded, failed = migrateAssetType(
					root,
					assetType,
					progressStart,
					span,
					nil,
					if index == 1 then scanEntries else nil
				)

				local attemptReports = { report }
				local finalReplaced = replaced
				local uploadedAcrossAttempts = uploaded
				local retriesUsed = 0

				while failed > 0 and retriesUsed < MAX_FAILED_ASSET_RETRIES do
					retriesUsed += 1
					totalRetries += 1

					local retryNotice = (
						"%s: %d unique asset(s) still failed. Automatic retry %d/%d; already successful assets will be reused from the cache."
					):format(
						ASSET_TYPE_LABELS[assetType] or assetType,
						failed,
						retriesUsed,
						MAX_FAILED_ASSET_RETRIES
					)

					setStatus("Retrying failed migration", retryNotice)
					warn("[RbxMigrator] " .. retryNotice)
					task.wait(0.5)

					local retryReport, retryReplaced, retryUploaded, retryFailed = migrateAssetType(
						root,
						assetType,
						progressStart,
						span,
						retriesUsed
					)

					table.insert(
						attemptReports,
						("AUTOMATIC RETRY %d/%d\n%s"):format(
							retriesUsed,
							MAX_FAILED_ASSET_RETRIES,
							retryReport
						)
					)

					-- Replaced references are taken from the final pass to avoid double-counting.
					finalReplaced = retryReplaced
					-- Successful IDs are cached, so each retry's new mappings are additional uploads.
					uploadedAcrossAttempts += retryUploaded
					failed = retryFailed
				end

				if retriesUsed > 0 then
					table.insert(
						attemptReports,
						1,
						("AUTOMATIC RETRY SUMMARY\nRetries attempted: %d\nUnique assets still failing: %d"):format(
							retriesUsed,
							failed
						)
					)
				end

				table.insert(reports, table.concat(attemptReports, "\n\n"))
				totalReplaced += finalReplaced
				totalUploaded += uploadedAcrossAttempts
				totalFailed += failed
			end

			setReport(table.concat(reports, "\n\n\n"))
			setProgress(1)
			updateSelectionPreview()

			if totalFailed > 0 then
				local retrySummary = if totalRetries > 0
					then (" after %d automatic retry attempt(s)"):format(totalRetries)
					else ""
				local detail = (
					"%d references replaced â¢ %d new assets uploaded â¢ %d unique assets still failed%s. See the migration report for IDs and errors."
				):format(
					totalReplaced,
					totalUploaded,
					totalFailed,
					retrySummary
				)
				setStatus("Migration partially complete", detail, true)
				warn("[RbxMigrator] Migration finished with " .. tostring(totalFailed)
					.. " unique asset(s) still failing after " .. tostring(totalRetries)
					.. " automatic retry attempt(s). Check the migration report for details.")
			elseif totalRetries > 0 then
				local detail = (
					"%d references replaced â¢ %d new unique assets uploaded â¢ all failed assets recovered after %d automatic retry attempt(s)."
				):format(totalReplaced, totalUploaded, totalRetries)
				setStatus("Migration complete after retries", detail)
				print("[RbxMigrator] " .. detail)
			else
				setStatus(
					"Migration complete",
					("%d references replaced â¢ %d new unique assets uploaded."):format(
						totalReplaced,
						totalUploaded
					)
				)
			end
		end)

		if not success then
			setStatus(
				"Migration failed",
				tostring(failure),
				true
			)
		end

		setBusy(false)
	end)
end

----------------------------------------------------------------
-- Cache-only tools
----------------------------------------------------------------

local function applyCachedMappings()
	if busy then return end
	local root = currentSelection()
	local scanEntries: {AssetManifestEntry}? = nil
	if root then
		local scanObjects = instancesIncludingRoot(root)
		local freshEntries, scanSources = scanAllReferences(root, scanObjects)
		scanEntries = freshEntries
		local fingerprint = previewFingerprint(root, scanObjects, freshEntries, scanSources)
		if previewRoot ~= root or previewSignature ~= fingerprint then
			previewSelected()
			setStatus("Review dry run before applying cache", "Preview updated. Read the plan, then click Apply Cache again to confirm.")
			return
		end
	end
	previewSignature = nil
	if not root then
		setStatus(
			"Select one item",
			"Select exactly one folder, model, or asset to continue.",
			true
		)
		return
	end

	local totalReplaced = 0
	local totalMissing = 0
	local usedAny = false

	for _, assetType in ipairs(ASSET_TYPE_ORDER) do
		if not enabledAssetTypes[assetType] then
			continue
		end

		local manifest = makeManifest(root, assetType, scanEntries)
		local ids = uniqueIds(manifest)
		local mapping: Mapping = {}

		for id in pairs(ids) do
			if cachedNewId(assetType, id) then
				mapping[id] = cachedNewId(assetType, id) :: string
			end
		end

		if next(mapping) ~= nil then
			usedAny = true
			local replaced = applyMapping(root, assetType, mapping)
			totalReplaced += replaced
		end

		totalMissing += countDictionary(ids) - countDictionary(mapping)
	end

	if not usedAny then
		setStatus(
			"No cached mappings",
			"The enabled asset types do not contain IDs stored in the plugin cache.",
			true
		)
		return
	end

	setProgress(1)
	setStatus(
		"Cached mappings applied",
		("%d reference(s) replaced. %d unique source ID(s) were not cached."):format(
			totalReplaced,
			totalMissing
		)
	)
	updateSelectionPreview()
end

local function clearCache()
	mappingCache = {}
	saveCache(mappingCache)

	setStatus(
		"Mapping cache cleared",
		"Future migrations will re-fetch and re-upload enabled assets until a new cache is built."
	)

	updateSelectionPreview()
end

----------------------------------------------------------------
-- Events
----------------------------------------------------------------

local function syncToolbarButton()
	pcall(function()
		toolbarButton:SetActive(widget.Enabled)
	end)
end

toolbarButton.Click:Connect(function()
	widget.Enabled = not widget.Enabled

	if widget.Enabled then
		-- Opening the plugin only refreshes the preview.
		-- Migration starts ONLY from the Migrate Selected button.
		updateSelectionPreview()
	end

	syncToolbarButton()
end)

widget:GetPropertyChangedSignal("Enabled"):Connect(function()
	syncToolbarButton()

	-- Pop the panel in whenever the window is opened.
	if widget.Enabled then
		rootScale.Scale = 0.96
		TweenService:Create(
			rootScale,
			TweenInfo.new(0.25, Enum.EasingStyle.Back, Enum.EasingDirection.Out),
			{ Scale = 1 }
		):Play()
	end
end)

previewUiButton.MouseButton1Click:Connect(function()
	previewSelected()
end)

migrateUiButton.MouseButton1Click:Connect(function()
	if busy then
		return
	end

	migrateSelected()
end)
applyCacheButton.MouseButton1Click:Connect(applyCachedMappings)
clearCacheButton.MouseButton1Click:Connect(clearCache)

animationTypeButton.MouseButton1Click:Connect(function()
	if busy then
		return
	end

	enabledAssetTypes.Animation = not enabledAssetTypes.Animation
	updateTypeButtons()
	updateSelectionPreview()
end)

soundTypeButton.MouseButton1Click:Connect(function()
	if busy then
		return
	end

	enabledAssetTypes.Audio = not enabledAssetTypes.Audio
	updateTypeButtons()
	updateSelectionPreview()
end)

meshTypeButton.MouseButton1Click:Connect(function()
	if busy then return end
	enabledAssetTypes.Mesh = not enabledAssetTypes.Mesh
	updateTypeButtons()
	updateSelectionPreview()
end)

forceButton.MouseButton1Click:Connect(function()
	forceReupload = not forceReupload

	forceButton.Text = if forceReupload
		then "â Force re-upload (ignore cache)"
		else "â Force re-upload (ignore cache)"

	TweenService:Create(
		forceButton,
		TweenInfo.new(0.16, Enum.EasingStyle.Quad, Enum.EasingDirection.Out),
		{
			BackgroundColor3 = if forceReupload
				then Color3.fromRGB(100, 66, 20)
				else Color3.fromRGB(48, 48, 48),
		}
	):Play()
end)

checkButton.MouseButton1Click:Connect(function()
	local newPort = tonumber(portBox.Text)

	if newPort
		and newPort % 1 == 0
		and newPort >= 1
		and newPort <= 65535
	then
		port = newPort
		plugin:SetSetting(PORT_SETTING, port)
	else
		portBox.Text = tostring(port)
	end

	checkHelper(true)
end)

portBox.FocusLost:Connect(function()
	local newPort = tonumber(portBox.Text)

	if newPort
		and newPort % 1 == 0
		and newPort >= 1
		and newPort <= 65535
	then
		port = newPort
		plugin:SetSetting(PORT_SETTING, port)
	else
		portBox.Text = tostring(port)
	end
end)

printReportButton.MouseButton1Click:Connect(function()
	print(reportBox.Text)
end)

refreshSelectionButton.MouseButton1Click:Connect(updateSelectionPreview)
Selection.SelectionChanged:Connect(updateSelectionPreview)

----------------------------------------------------------------
-- Initial state
----------------------------------------------------------------

updateTypeButtons()
updateSelectionPreview()
setProgress(0)
setStatus(
	"Ready",
	"Choose types and click Preview / Dry Run. Nothing uploads until you confirm migration."
)

syncToolbarButton()

task.defer(function()
	checkHelper(false)
end)

widget.Enabled = true