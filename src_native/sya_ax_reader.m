#import <Cocoa/Cocoa.h>
#import <ApplicationServices/ApplicationServices.h>

/**
 * Native macOS Accessibility Reader for SYA.
 * Implements Apple Developer Accessibility API (AXUIElement / ApplicationServices):
 * - Identifies frontmost application via NSWorkspace
 * - Retrieves active focused window (kAXFocusedWindowAttribute / kAXMainWindowAttribute)
 * - Reads window topic and document (kAXTitleAttribute, kAXDocumentAttribute, kAXURLAttribute)
 * - Reads active semantic content from focused element (kAXFocusedUIElementAttribute -> kAXSelectedTextAttribute / kAXValueAttribute)
 * - Safely traverses visible window elements within strict bounds, respecting privacy guards (secure fields).
 */

static NSString *sanitizeString(NSString *str, NSUInteger maxLen) {
    if (!str) return @"";
    NSString *clean = [[str stringByReplacingOccurrencesOfString:@"\r\n" withString:@"\n"]
                       stringByReplacingOccurrencesOfString:@"\r" withString:@"\n"];
    clean = [clean stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
    if (clean.length > maxLen) {
        clean = [clean substringToIndex:maxLen];
    }
    return clean;
}

static void traverseChildren(AXUIElementRef node, NSMutableArray<NSString *> *chunks, NSUInteger *totalLen, NSUInteger maxLen, int depth) {
    if (depth > 6 || *totalLen >= maxLen) return;

    CFTypeRef childrenRef = NULL;
    AXError err = AXUIElementCopyAttributeValue(node, kAXVisibleChildrenAttribute, &childrenRef);
    if (err != kAXErrorSuccess || !childrenRef) {
        err = AXUIElementCopyAttributeValue(node, kAXChildrenAttribute, &childrenRef);
    }

    if (err == kAXErrorSuccess && childrenRef) {
        NSArray *children = (__bridge NSArray *)childrenRef;
        NSUInteger count = children.count;
        for (NSUInteger i = 0; i < count && *totalLen < maxLen; i++) {
            AXUIElementRef child = (__bridge AXUIElementRef)children[i];
            
            CFTypeRef roleRef = NULL;
            AXUIElementCopyAttributeValue(child, kAXRoleAttribute, &roleRef);
            NSString *role = roleRef ? (__bridge NSString *)roleRef : @"";

            CFTypeRef subroleRef = NULL;
            AXUIElementCopyAttributeValue(child, kAXSubroleAttribute, &subroleRef);
            NSString *subrole = subroleRef ? (__bridge NSString *)subroleRef : @"";

            BOOL isSecure = [role containsString:@"Secure"] || [subrole containsString:@"Secure"] ||
                            [role containsString:@"Password"] || [subrole containsString:@"Password"];

            if (!isSecure) {
                if ([role isEqualToString:@"AXStaticText"] || [role isEqualToString:@"AXHeading"]) {
                    CFTypeRef valRef = NULL;
                    if (AXUIElementCopyAttributeValue(child, kAXValueAttribute, &valRef) == kAXErrorSuccess && valRef) {
                        if ([(__bridge id)valRef isKindOfClass:[NSString class]]) {
                            NSString *t = [(__bridge NSString *)valRef stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
                            if (t.length > 0) {
                                [chunks addObject:t];
                                *totalLen += t.length;
                            }
                        }
                        CFRelease(valRef);
                    }
                } else if ([role isEqualToString:@"AXTextArea"] || [role isEqualToString:@"AXTextField"] || [role isEqualToString:@"AXWebArea"]) {
                    CFTypeRef valRef = NULL;
                    if (AXUIElementCopyAttributeValue(child, kAXValueAttribute, &valRef) == kAXErrorSuccess && valRef) {
                        if ([(__bridge id)valRef isKindOfClass:[NSString class]]) {
                            NSString *t = [(__bridge NSString *)valRef stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
                            if (t.length > 0) {
                                [chunks addObject:t];
                                *totalLen += t.length;
                            }
                        }
                        CFRelease(valRef);
                    }
                }
            }

            if (roleRef) CFRelease(roleRef);
            if (subroleRef) CFRelease(subroleRef);

            traverseChildren(child, chunks, totalLen, maxLen, depth + 1);
        }
        CFRelease(childrenRef);
    }
}

int main(int argc, const char * argv[]) {
    @autoreleasepool {
        Boolean trusted = AXIsProcessTrusted();

        pid_t pid = 0;
        NSString *appName = @"";
        NSString *bundleId = @"";

        if (argc > 1) {
            pid = (pid_t)atoi(argv[1]);
            NSRunningApplication *app = [NSRunningApplication runningApplicationWithProcessIdentifier:pid];
            if (app) {
                appName = [app localizedName] ?: @"Unknown";
                bundleId = [app bundleIdentifier] ?: @"";
            }
        }

        if (pid <= 0) {
            NSRunningApplication *frontApp = [[NSWorkspace sharedWorkspace] frontmostApplication];
            if (frontApp) {
                pid = [frontApp processIdentifier];
                appName = [frontApp localizedName] ?: @"Unknown";
                bundleId = [frontApp bundleIdentifier] ?: @"";
            }
        }

        if (pid <= 0) {
            printf("{\"ok\":false,\"error\":\"NO_FOREGROUND_APP\",\"isTrusted\":%s}\n", trusted ? "true" : "false");
            return 0;
        }

        AXUIElementRef appElem = AXUIElementCreateApplication(pid);
        if (!appElem) {
            printf("{\"ok\":false,\"error\":\"AX_APP_FAILED\",\"appName\":\"%s\",\"bundleId\":\"%s\",\"pid\":%d,\"isTrusted\":%s}\n",
                   [appName UTF8String], [bundleId UTF8String], pid, trusted ? "true" : "false");
            return 0;
        }

        AXUIElementRef winElem = NULL;
        AXError err = AXUIElementCopyAttributeValue(appElem, kAXFocusedWindowAttribute, (CFTypeRef *)&winElem);
        if (err != kAXErrorSuccess || !winElem) {
            err = AXUIElementCopyAttributeValue(appElem, kAXMainWindowAttribute, (CFTypeRef *)&winElem);
        }
        if (err != kAXErrorSuccess || !winElem) {
            CFArrayRef windowList = NULL;
            if (AXUIElementCopyAttributeValue(appElem, kAXWindowsAttribute, (CFTypeRef *)&windowList) == kAXErrorSuccess && windowList) {
                if (CFArrayGetCount(windowList) > 0) {
                    winElem = (AXUIElementRef)CFRetain(CFArrayGetValueAtIndex(windowList, 0));
                }
                CFRelease(windowList);
            }
        }

        NSString *windowTitle = @"";
        NSString *documentUrl = @"";
        NSString *pageUrl = @"";
        NSString *semanticText = @"";

        if (winElem) {
            CFTypeRef titleRef = NULL;
            if (AXUIElementCopyAttributeValue(winElem, kAXTitleAttribute, &titleRef) == kAXErrorSuccess && titleRef) {
                if ([(__bridge id)titleRef isKindOfClass:[NSString class]]) {
                    windowTitle = [(__bridge NSString *)titleRef copy];
                }
                CFRelease(titleRef);
            }

            CFTypeRef docRef = NULL;
            if (AXUIElementCopyAttributeValue(winElem, kAXDocumentAttribute, &docRef) == kAXErrorSuccess && docRef) {
                if ([(__bridge id)docRef isKindOfClass:[NSString class]]) {
                    documentUrl = [(__bridge NSString *)docRef copy];
                }
                CFRelease(docRef);
            }

            CFTypeRef urlRef = NULL;
            if (AXUIElementCopyAttributeValue(winElem, (CFStringRef)@"AXURL", &urlRef) == kAXErrorSuccess && urlRef) {
                if ([(__bridge id)urlRef isKindOfClass:[NSString class]]) {
                    pageUrl = [(__bridge NSString *)urlRef copy];
                }
                CFRelease(urlRef);
            }

            if (windowTitle.length == 0) {
                CFTypeRef descRef = NULL;
                if (AXUIElementCopyAttributeValue(winElem, kAXDescriptionAttribute, &descRef) == kAXErrorSuccess && descRef) {
                    if ([(__bridge id)descRef isKindOfClass:[NSString class]]) {
                        windowTitle = [(__bridge NSString *)descRef copy];
                    }
                    CFRelease(descRef);
                }
            }

            // If title is empty but documentUrl exists, use filename as window subject
            if (windowTitle.length == 0 && documentUrl.length > 0) {
                NSURL *u = [NSURL URLWithString:documentUrl];
                if (u && u.lastPathComponent) {
                    windowTitle = u.lastPathComponent;
                }
            }

            // 1. Check focused element for user active text
            AXUIElementRef focusedElem = NULL;
            if (AXUIElementCopyAttributeValue(appElem, kAXFocusedUIElementAttribute, (CFTypeRef *)&focusedElem) != kAXErrorSuccess || !focusedElem) {
                AXUIElementCopyAttributeValue(winElem, kAXFocusedUIElementAttribute, (CFTypeRef *)&focusedElem);
            }

            if (focusedElem) {
                CFTypeRef roleRef = NULL;
                AXUIElementCopyAttributeValue(focusedElem, kAXRoleAttribute, &roleRef);
                NSString *role = roleRef ? (__bridge NSString *)roleRef : @"";

                CFTypeRef subroleRef = NULL;
                AXUIElementCopyAttributeValue(focusedElem, kAXSubroleAttribute, &subroleRef);
                NSString *subrole = subroleRef ? (__bridge NSString *)subroleRef : @"";

                BOOL isSecure = [role containsString:@"Secure"] || [subrole containsString:@"Secure"] ||
                                [role containsString:@"Password"] || [subrole containsString:@"Password"];

                if (!isSecure) {
                    CFTypeRef selRef = NULL;
                    if (AXUIElementCopyAttributeValue(focusedElem, kAXSelectedTextAttribute, &selRef) == kAXErrorSuccess && selRef) {
                        if ([(__bridge id)selRef isKindOfClass:[NSString class]]) {
                            NSString *sel = [(__bridge NSString *)selRef stringByTrimmingCharactersInSet:[NSCharacterSet whitespaceAndNewlineCharacterSet]];
                            if (sel.length > 0) {
                                semanticText = [NSString stringWithFormat:@"[Selected]: %@", sel];
                            }
                        }
                        CFRelease(selRef);
                    }

                    if (semanticText.length == 0) {
                        CFTypeRef valRef = NULL;
                        if (AXUIElementCopyAttributeValue(focusedElem, kAXValueAttribute, &valRef) == kAXErrorSuccess && valRef) {
                            if ([(__bridge id)valRef isKindOfClass:[NSString class]]) {
                                semanticText = [(__bridge NSString *)valRef copy];
                            } else if ([(__bridge id)valRef isKindOfClass:[NSAttributedString class]]) {
                                semanticText = [[(__bridge NSAttributedString *)valRef string] copy];
                            }
                            CFRelease(valRef);
                        }
                    }

                    if (semanticText.length == 0) {
                        CFTypeRef titleValRef = NULL;
                        if (AXUIElementCopyAttributeValue(focusedElem, kAXTitleAttribute, &titleValRef) == kAXErrorSuccess && titleValRef) {
                            if ([(__bridge id)titleValRef isKindOfClass:[NSString class]]) {
                                semanticText = [(__bridge NSString *)titleValRef copy];
                            }
                            CFRelease(titleValRef);
                        }
                    }
                }

                if (roleRef) CFRelease(roleRef);
                if (subroleRef) CFRelease(subroleRef);
                CFRelease(focusedElem);
            }

            // 2. If semantic text is still empty, traverse window children up to 1200 chars
            if (semanticText.length == 0) {
                NSMutableArray<NSString *> *chunks = [NSMutableArray array];
                NSUInteger total = 0;
                traverseChildren(winElem, chunks, &total, 1200, 0);
                if (chunks.count > 0) {
                    semanticText = [chunks componentsJoinedByString:@"\n"];
                }
            }

            CFRelease(winElem);
        }

        CFRelease(appElem);

        windowTitle = sanitizeString(windowTitle, 300);
        documentUrl = sanitizeString(documentUrl, 500);
        pageUrl = sanitizeString(pageUrl, 500);
        semanticText = sanitizeString(semanticText, 1200);

        NSMutableDictionary *dict = [NSMutableDictionary dictionary];
        dict[@"ok"] = @YES;
        dict[@"appName"] = appName;
        dict[@"bundleId"] = bundleId;
        dict[@"pid"] = @(pid);
        dict[@"windowTitle"] = windowTitle;
        dict[@"documentUrl"] = documentUrl;
        dict[@"pageUrl"] = pageUrl;
        dict[@"semanticText"] = semanticText;
        dict[@"isTrusted"] = @(trusted);

        NSError *jsonError = nil;
        NSData *jsonData = [NSJSONSerialization dataWithJSONObject:dict options:0 error:&jsonError];
        if (jsonData) {
            NSString *jsonString = [[NSString alloc] initWithData:jsonData encoding:NSUTF8StringEncoding];
            printf("%s\n", [jsonString UTF8String]);
        } else {
            printf("{\"ok\":false,\"error\":\"JSON_SERIALIZATION_FAILED\"}\n");
        }
    }
    return 0;
}
