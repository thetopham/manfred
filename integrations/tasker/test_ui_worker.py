"""Execute the production BeanShell worker against small deterministic Android fakes.

Set MANFRED_TASKER_JAVA_CACHE to a task-local JRE + bsh.jar/json.jar/ecj.jar.
No downloads, Android build, real UI, files on a phone, or network calls in tests.
"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

HERE = Path(__file__).resolve().parent
WORKER = HERE / "eyevue-ui-worker.java.txt"

STUBS = {
    "android/os/SystemClock.java": """package android.os; public class SystemClock { public static long now=1000; public static long elapsedRealtime(){return now++;} public static void sleep(long ms){now+=ms;} }""",
    "android/os/Looper.java": """package android.os; public class Looper { static final Looper MAIN=new Looper(); public static Looper myLooper(){return null;} public static Looper getMainLooper(){return MAIN;} }""",
    "android/os/Environment.java": """package android.os; import java.io.*; public class Environment { public static File getExternalStorageDirectory(){return new File(System.getProperty(\"mq.test.tmpdir\"));} }""",
    "android/os/Handler.java": """package android.os; public class Handler { public Handler(Looper l){} }""",
    "android/os/Bundle.java": """package android.os; public class Bundle { public void putCharSequence(String k,CharSequence v){} }""",
    "android/graphics/Rect.java": """package android.graphics; public class Rect { public int left,top,right,bottom; public boolean isEmpty(){return right<=left||bottom<=top;} public int centerX(){return (left+right)/2;} public int centerY(){return (top+bottom)/2;} public boolean contains(int x,int y){return x>=left&&x<right&&y>=top&&y<bottom;} public String flattenToString(){return left+\" \"+top+\" \"+right+\" \"+bottom;} }""",
    "android/graphics/Path.java": """package android.graphics; public class Path { public void moveTo(float x,float y){} }""",
    "android/graphics/BitmapFactory.java": """package android.graphics; import java.io.*; public class BitmapFactory { public static int width=3200,height=2400; public static class Options { public boolean inJustDecodeBounds; public int outWidth,outHeight; } public static Object decodeStream(InputStream s,Object r,Options o){o.outWidth=width;o.outHeight=height;return null;} }""",
    "android/net/Uri.java": """package android.net; public class Uri { public String value; public static Uri parse(String v){Uri u=new Uri();u.value=v;return u;} }""",
    "android/net/Network.java": """package android.net; public class Network {}""",
    "android/net/NetworkCapabilities.java": """package android.net; public class NetworkCapabilities { public static final int NET_CAPABILITY_INTERNET=12,NET_CAPABILITY_VALIDATED=16; public static boolean validated=true; public static long validAfter=0; public boolean hasCapability(int c){return c!=16||validated||(validAfter>0&&android.os.SystemClock.now>=validAfter);} }""",
    "android/net/ConnectivityManager.java": """package android.net; public class ConnectivityManager { public Network getActiveNetwork(){return new Network();} public NetworkCapabilities getNetworkCapabilities(Network n){return new NetworkCapabilities();} }""",
    "android/provider/OpenableColumns.java": """package android.provider; public class OpenableColumns { public static final String DISPLAY_NAME=\"_display_name\",SIZE=\"_size\"; }""",
    "android/database/Cursor.java": """package android.database; public class Cursor { public String name; public long size; public Cursor(String n,long s){name=n;size=s;} public int getCount(){return 1;} public boolean moveToFirst(){return true;} public int getColumnIndexOrThrow(String n){return n.equals(\"_size\")?1:0;} public String getString(int i){return name;} public boolean isNull(int i){return false;} public long getLong(int i){return size;} public void close(){} }""",
    "android/content/ContentResolver.java": """package android.content; import android.database.*;import android.net.*;import java.io.*; public class ContentResolver { public byte[] bytes={1,2,3,4,5}; public String name; public int reads=0; public String getType(Uri u){return \"image/jpeg\";} public Cursor query(Uri u,String[] p,String q,String[] a,String o){return new Cursor(name,bytes.length);} public InputStream openInputStream(Uri u){reads++;return new ByteArrayInputStream(bytes);} }""",
    "android/app/KeyguardManager.java": """package android.app; public class KeyguardManager { public static boolean locked=false; public boolean isKeyguardLocked(){return locked;} public boolean isDeviceLocked(){return locked;} }""",
    "android/content/Context.java": """package android.content; import android.net.*; public class Context { public static final String CONNECTIVITY_SERVICE=\"connectivity\",KEYGUARD_SERVICE=\"keyguard\"; public ContentResolver resolver=new ContentResolver(); public ContentResolver getContentResolver(){return resolver;} public Object getSystemService(String s){return s.equals(KEYGUARD_SERVICE)?new android.app.KeyguardManager():new ConnectivityManager();} }""",
    "android/accessibilityservice/AccessibilityServiceInfo.java": """package android.accessibilityservice; public class AccessibilityServiceInfo { public static final int FLAG_RETRIEVE_INTERACTIVE_WINDOWS=64,CAPABILITY_CAN_RETRIEVE_WINDOW_CONTENT=1,CAPABILITY_CAN_PERFORM_GESTURES=32; public int flags=83; public int getCapabilities(){return 33;} }""",
    "android/accessibilityservice/AccessibilityService.java": """package android.accessibilityservice; import java.util.*; import android.os.*; public class AccessibilityService { public List windows=new ArrayList(); public int globalBacks=0,gestureCalls=0; public boolean backAllowed=true,backChanges=true; public static final int GLOBAL_ACTION_BACK=1; public boolean performGlobalAction(int action){globalBacks++;if(!backAllowed)return false;if(backChanges&&windows.size()>1)windows.remove(1);return true;} public List getWindows(){return windows;} public AccessibilityServiceInfo getServiceInfo(){return new AccessibilityServiceInfo();} public static class GestureResultCallback {} public boolean dispatchGesture(GestureDescription g,Object c,Handler h){gestureCalls++;throw new AssertionError(\"Real gesture must be mocked\");} }""",
    "android/accessibilityservice/GestureDescription.java": """package android.accessibilityservice; import android.graphics.*; public class GestureDescription { public static class StrokeDescription { public StrokeDescription(Path p,long s,long d){} } public static class Builder { public Builder addStroke(StrokeDescription s){return this;} public GestureDescription build(){return new GestureDescription();} } }""",
    "android/view/accessibility/AccessibilityNodeInfo.java": """package android.view.accessibility; import java.util.*; import android.graphics.*;import android.os.*; public class AccessibilityNodeInfo { public String pkg=\"com.openai.chatgpt\",text=null,description=null,id=null,unique=null;public boolean editable=false,visible=true,enabled=true,focused=false,focusAllowed=true,clickable=false; public int focusRequests=0,clickRequests=0;public boolean clickAllowed=true;public List children=new ArrayList(); public int x=0,y=10,width=50,height=50; public long visibleUntil=0,visibleAfter=0,clickReadyAfter=0; public AccessibilityNodeInfo parent=null; public AccessibilityNodeInfo getParent(){return parent;} public CharSequence getPackageName(){return pkg;}public CharSequence getClassName(){return \"android.view.View\";}public CharSequence getText(){return text;}public CharSequence getContentDescription(){return description;}public String getViewIdResourceName(){return id;}public String getUniqueId(){return unique;}public boolean isEditable(){return editable;}public boolean isClickable(){return clickable&&(clickReadyAfter==0||android.os.SystemClock.now>=clickReadyAfter);}public boolean isFocused(){return focused;}public boolean isVisibleToUser(){return visible&&(visibleUntil==0||android.os.SystemClock.now<visibleUntil)&&(visibleAfter==0||android.os.SystemClock.now>=visibleAfter);}public boolean isEnabled(){return enabled;}public int getChildCount(){return children.size();}public AccessibilityNodeInfo getChild(int i){return (AccessibilityNodeInfo)children.get(i);}public void getBoundsInScreen(Rect r){r.left=x;r.top=y;r.right=x+width;r.bottom=y+height;}public boolean refresh(){return true;}public boolean performAction(int a,Bundle b){return true;}public boolean performAction(int a){if(a==ACTION_CLICK){clickRequests++;return clickAllowed;}if(a==ACTION_FOCUS){focusRequests++;if(!focusAllowed)return false;focused=true;}return true;} public List getActionList(){List actions=new ArrayList();if(isClickable())actions.add(new AccessibilityAction(ACTION_CLICK));return actions;} public static final int ACTION_FOCUS=1,ACTION_CLICK=16,ACTION_SET_TEXT=2097152;public static final String ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE=\"text\";public static class AccessibilityAction {private int actionId;public AccessibilityAction(){this(2);}public AccessibilityAction(int id){actionId=id;}public static final AccessibilityAction ACTION_IME_ENTER=new AccessibilityAction();public int getId(){return actionId;}} }""",
    "android/view/accessibility/AccessibilityWindowInfo.java": """package android.view.accessibility;import android.graphics.*; public class AccessibilityWindowInfo {public AccessibilityNodeInfo root;public int id=1,layer=0,type=1;public boolean isActive(){return false;}public boolean isFocused(){return false;}public int getType(){return type;}public AccessibilityNodeInfo getRoot(){return root;}public int getId(){return id;}public int getLayer(){return layer;}public void getBoundsInScreen(Rect r){r.left=0;r.top=0;r.right=1000;r.bottom=2000;} }""",
    "com/joaomgcd/taskerm/action/java/ClassImplementation.java": """package com.joaomgcd.taskerm.action.java; import java.util.concurrent.*; public interface ClassImplementation {Object run(Callable c,String n,Object[] a) throws Exception;}""",
}

DRIVER = r'''
import bsh.Interpreter;
import java.nio.file.*;
import java.util.*;
import java.security.*;
import org.json.*;
import android.content.*;
import android.accessibilityservice.*;
import android.view.accessibility.*;

public class WorkerHarness {
 public static class FakeTasker {
  public Map<String,String> vars=new HashMap<String,String>();
  public AccessibilityService service=new AccessibilityService();
  public List<String> taps=new ArrayList<String>();
  public String fileName,phase="chat",scenario;
  public long sendTime=0;
  public boolean normalized=false;
  public String getVariable(String n){return vars.get(n);}
  public void setVariable(String n,Object v){vars.put(n,String.valueOf(v));}
  public AccessibilityService getAccessibilityService(){layout();return service;}
  public Object implementClass(Class c,Object implementation){return null;}
  public AccessibilityNodeInfo node(String label,String pkg,int x) {AccessibilityNodeInfo n=new AccessibilityNodeInfo();n.text=label;n.pkg=pkg;n.x=x;return n;}
  public void layout(){
   AccessibilityNodeInfo root=new AccessibilityNodeInfo();
   String pkg="com.openai.chatgpt";
   String[] labels;
   if(phase.equals("chat")) labels=(scenario.equals("existing_draft")||scenario.equals("stale_popup_existing_draft"))?new String[]{"End","Attachment","Remove"}:new String[]{"End","Attachment"};
   else if(phase.equals("menu")) labels=new String[]{"Files"};
   else if(phase.equals("chooser")) labels=new String[]{"Upload files"};
   else if(phase.equals("picker")){pkg="com.google.android.documentsui";labels=new String[]{fileName};}
   else if(phase.equals("draft")) labels=scenario.equals("preview_timeout")?new String[]{}:new String[]{"Remove","Send Message"};
   else if(phase.equals("voice"))labels=new String[]{"End","Attachment"};
   else if(phase.equals("sent_focus"))labels=scenario.equals("attach_focus_conflict")?new String[]{"End","toggle focus mode","Copy"}:scenario.equals("attach_focus_disabled_image")?new String[]{"End","toggle focus mode","Image"}:new String[]{"End","toggle focus mode"};
   else if(phase.equals("sent_transcript")){
    if(scenario.equals("attach_focus_uploading")||scenario.equals("attach_focus_upload_clears"))labels=new String[]{"End","toggle focus mode","Image","Uploading attachment"};
    else if(scenario.equals("attach_focus_unsent"))labels=new String[]{"End","toggle focus mode","Image","Unsent \u00b7 Retry","Retry","Retry","Looks like some messages never sent. Retry?"};
    else labels=new String[]{"End","toggle focus mode","Image"};
   }
   else labels=scenario.equals("attach_error_ui")?new String[]{"Start a voice conversation","Image","Retry"}:scenario.equals("attach_poor_connection")?new String[]{"Start a voice conversation","Image","Poor connection"}:new String[]{"Start a voice conversation","Image"};
   if(phase.equals("chat") && (scenario.equals("attach_normalize_small")||scenario.equals("attach_normalize_history_no_new")||scenario.equals("attach_normalize_large")||scenario.equals("prepare_unknown_small"))) labels=new String[]{"End","Attachment","toggle focus mode"};
   if(phase.equals("chat") && scenario.equals("prepare_empty_transcript")) labels=new String[]{"End","Attachment"};
   if(phase.equals("chat") && scenario.equals("prepare_voice_inactive")) labels=new String[]{"Start a voice conversation","Attachment"};
   root.pkg=pkg;
   boolean transcript=phase.equals("chat")||phase.equals("voice")||phase.equals("draft")||phase.equals("sent")||phase.equals("sent_transcript");
   if(phase.equals("chat")&&(scenario.equals("attach_normalize_small")||scenario.equals("attach_normalize_history_no_new")||scenario.equals("attach_normalize_large")||scenario.equals("prepare_unknown_small")||scenario.equals("prepare_empty_transcript")))transcript=false;
   if(phase.equals("chat")&&scenario.equals("prepare_controls_other_window"))transcript=false;
   if(phase.equals("sent_focus")&&(scenario.equals("attach_focus_small_wait")||scenario.equals("attach_focus_disabled_image")))transcript=true;
   if(phase.equals("draft")&&scenario.equals("attach_presentation_changed_before_send"))transcript=false;
   if(transcript)root.children.add(node("Copy",pkg,850));
   if(normalized&&phase.equals("sent_transcript"))root.children.add(node("Attachment",pkg,920));
   if(phase.equals("draft"))root.children.add(node("Start a voice conversation",pkg,900));
   for(int i=0;i<labels.length;i++) {
    AccessibilityNodeInfo n=node(labels[i],pkg,i*60);
    if(labels[i].equals("Uploading attachment")&&scenario.equals("attach_focus_upload_clears"))n.visibleUntil=android.os.SystemClock.now+1000;
    if(labels[i].equals("toggle focus mode")){
     boolean small=phase.equals("sent_transcript")||scenario.equals("attach_normalize_small")||scenario.equals("attach_normalize_history_no_new")||scenario.equals("prepare_unknown_small")||scenario.equals("attach_focus_small_wait")||scenario.equals("attach_focus_small_empty")||scenario.equals("attach_focus_disabled_image");
     n.x=small?350:100;n.y=small?1500:500;n.width=small?300:700;n.height=n.width;
    }
    if(labels[i].equals("Image")&&scenario.equals("attach_focus_disabled_image"))n.enabled=false;
    if(labels[i].equals("Send Message")){
     AccessibilityNodeInfo button=node(null,pkg,500);button.clickable=true;
     if(scenario.equals("attach_send_delayed_ready"))button.clickReadyAfter=android.os.SystemClock.now+900;
     if(scenario.equals("attach_send_never_ready"))button.clickReadyAfter=android.os.SystemClock.now+10000;
     button.children.add(n);n.parent=button;root.children.add(button);button.parent=root;
    } else {root.children.add(n);n.parent=root;}
   }
   if(phase.equals("draft")&&(scenario.equals("attach_old_image")||scenario.equals("attach_delayed_new_image")||scenario.equals("attach_normalize_history_no_new")))root.children.add(node("Image",pkg,300));
   if(phase.equals("sent")&&scenario.equals("attach_delayed_new_image")){AccessibilityNodeInfo newImage=node("Image",pkg,700);newImage.visibleAfter=sendTime+1800;root.children.add(newImage);}
   if(phase.equals("sent_focus")&&scenario.equals("attach_focus_small_wait")){AccessibilityNodeInfo newImage=node("Image",pkg,700);newImage.visibleAfter=sendTime+2000;root.children.add(newImage);}
   AccessibilityWindowInfo w=new AccessibilityWindowInfo();w.root=root;if(phase.equals("sent")&&scenario.equals("attach_new_window"))w.id=2;service.windows.clear();service.windows.add(w);
   if(phase.equals("chat")&&scenario.equals("prepare_controls_other_window")){AccessibilityWindowInfo other=new AccessibilityWindowInfo();other.id=7;other.root=node("Copy",pkg,0);service.windows.add(other);}
   if(scenario.equals("occluded")){AccessibilityWindowInfo blocker=new AccessibilityWindowInfo();blocker.id=8;blocker.layer=3;blocker.type=3;blocker.root=node("UNRELATED PRIVATE TITLE","com.example.overlay",0);service.windows.add(blocker);}
   if(phase.equals("chat")&&(scenario.startsWith("stale_popup")||scenario.equals("unknown_popup"))){
    AccessibilityWindowInfo popup=new AccessibilityWindowInfo();popup.id=2221;popup.layer=1;popup.root=new AccessibilityNodeInfo();popup.root.children.add(node("Files","com.openai.chatgpt",0));
    if(!scenario.equals("unknown_popup")){popup.root.children.add(node("Camera","com.openai.chatgpt",60));popup.root.children.add(node("Photos","com.openai.chatgpt",120));}
    service.windows.add(popup);service.backAllowed=!scenario.equals("stale_popup_back_denied");service.backChanges=!scenario.equals("stale_popup_back_stuck");
   }
  }
  public void tap(String kind,String value){
   taps.add(value);
   if(value.equals("Attachment"))phase="menu";
   else if(value.equals("Files"))phase="chooser";
   else if(value.equals("Upload files"))phase="picker";
   else if(value.equals(fileName)){phase="draft";if(scenario.equals("selection_uncertain"))throw new IllegalStateException("mq:gesture_callback_timeout");}
   else if(value.equals("Send Message")){sendTime=android.os.SystemClock.now;phase=scenario.startsWith("attach_focus_")?"sent_focus":"sent";if(scenario.equals("attach_focus_uploading")||scenario.equals("attach_focus_upload_clears")||scenario.equals("attach_focus_unsent"))phase="sent_transcript";if(scenario.equals("attach_network_lost"))android.net.NetworkCapabilities.validated=false;}
   else if(value.equals("Start a voice conversation")){if(scenario.equals("attach_resume_failed"))throw new IllegalStateException("mq:gesture_callback_timeout");phase="voice";}
   else if(value.equals("toggle focus mode")){if(!scenario.equals("prepare_unknown_small")){phase="sent_transcript";normalized=true;}}
   layout();
  }
 }
 public static void main(String[] args)throws Exception {
  new java.io.File(android.os.Environment.getExternalStorageDirectory(),"Tasker").mkdirs();
  String code=new String(Files.readAllBytes(Paths.get(args[0])),"UTF-8");
  int split=code.indexOf("String stage = mqValue(\"mq_stage\");");
  if(split<0)throw new AssertionError("Entry marker missing");
  String defs=code.substring(0,split),entry=code.substring(split);
  FakeTasker tasker=new FakeTasker();tasker.scenario=args[1];
  Context context=new Context();
  String id="11111111-1111-4111-8111-111111111111",owner="worker_test_123";
  String fileName="EyeVue_1700000000000_11111111.jpg";
  tasker.fileName=fileName;context.resolver.name=fileName;
  byte[] hash=MessageDigest.getInstance("SHA-256").digest(context.resolver.bytes);
  StringBuilder hex=new StringBuilder();for(byte b:hash)hex.append(String.format("%02x",b&255));
  JSONObject image=new JSONObject().put("id",id).put("sessionId",id).put("uri","content://media/external/images/media/7").put("fileName",fileName).put("bytes",5).put("width",3200).put("height",2400).put("sha256",hex.toString());
  JSONObject item=new JSONObject().put("image",image).put("owner",owner).put("state","claimed");
  JSONObject queue=new JSONObject().put("version",1).put("items",new JSONArray().put(item));
  tasker.vars.put("ManfredEyevueQueue",queue.toString());
  tasker.vars.put("mq_id",id);tasker.vars.put("mq_owner",owner);
  String[] fields={"uri","fileName","bytes","width","height","sha256"},locals={"mq_uri","mq_filename","mq_bytes","mq_width","mq_height","mq_sha256"};
  for(int i=0;i<fields.length;i++)tasker.vars.put(locals[i],String.valueOf(image.get(fields[i])));
  tasker.vars.put("mq_stage","prepare");tasker.vars.put("mq_ok","1");tasker.vars.put("mq_status","claimed");
  if(args[1].equals("no_gate"))tasker.vars.put("mq_ok","0");
  if(args[1].equals("wrong_owner"))tasker.vars.put("mq_owner","worker_other_123");
  if(args[1].equals("local_mismatch"))tasker.vars.put("mq_bytes","6");
  if(args[1].equals("metadata_mismatch"))context.resolver.name="other.jpg";
  if(args[1].equals("bytes_mismatch"))context.resolver.bytes[0]=9;
  if(args[1].equals("dimensions_mismatch"))android.graphics.BitmapFactory.width=640;
  if(args[1].equals("phone_locked"))android.app.KeyguardManager.locked=true;
  if(args[1].equals("prepare_offline")||args[1].equals("prepare_network_returns"))android.net.NetworkCapabilities.validated=false;
  if(args[1].equals("prepare_network_returns"))android.net.NetworkCapabilities.validAfter=2000;
  // Replace only the dispatch helper before interpretation. BeanShell retains
  // overloaded definitions, so redefining afterward is not a reliable mock.
  int tapStart=defs.indexOf("void mqTap(");
  int tapEnd=defs.indexOf("// Only anonymous Image-node signatures");
  if(tapStart<0||tapEnd<tapStart)throw new AssertionError("Dispatch marker missing");
  if(!args[1].equals("occluded")&&!args[1].equals("unknown_popup")&&!args[1].startsWith("file_click_")&&!args[1].startsWith("send_click_"))defs=defs.substring(0,tapStart)+"void mqTap(AccessibilityService s,String p,String k,String v){tasker.tap(k,v);}"+defs.substring(tapEnd);
  Interpreter bsh=new Interpreter();bsh.set("tasker",tasker);bsh.set("context",context);bsh.eval(defs);
  tasker.layout();
  if(args[1].startsWith("send_click_")){
   String pkg="com.openai.chatgpt";
   AccessibilityNodeInfo exact=tasker.node("Send Message",pkg,400);
   AccessibilityNodeInfo button=tasker.node(null,pkg,50);button.clickable=true;button.children.add(exact);exact.parent=button;
   AccessibilityNodeInfo composer=tasker.node(null,pkg,10);composer.children.add(button);button.parent=composer;composer.children.add(tasker.node("Attachment",pkg,600));
   if(args[1].equals("send_click_rejected"))button.clickAllowed=false;
   if(args[1].equals("send_click_extra_label"))button.children.add(tasker.node("Attachment",pkg,500));
   if(args[1].equals("send_click_duplicate"))button.children.add(tasker.node("Send Message",pkg,500));
   if(args[1].equals("send_click_composer_only")){button.clickable=false;composer.clickable=true;}
   if(args[1].equals("send_click_wrong_package"))button.pkg="other.package";
   if(args[1].equals("send_click_disabled"))button.enabled=false;
   AccessibilityWindowInfo window=new AccessibilityWindowInfo();window.root=composer;window.id=501;tasker.service.windows.clear();tasker.service.windows.add(window);
   boolean rejected=false;try{bsh.eval("mqTap(tasker.service,\"com.openai.chatgpt\",\"label\",\"Send Message\");");}catch(bsh.TargetError failure){rejected=true;}
   System.out.println(new JSONObject().put("rejected",rejected).put("clickRequests",button.clickRequests).put("composerClicks",composer.clickRequests).put("gestureCalls",tasker.service.gestureCalls).put("trace",bsh.get("mqTapAttempts")));return;
  }
  if(args[1].startsWith("tile_")||args[1].startsWith("file_click_")){
   String pkg="com.google.android.documentsui";
   AccessibilityNodeInfo exact=tasker.node(fileName,pkg,400);
   AccessibilityNodeInfo row=tasker.node(null,pkg,50);row.id=pkg+":id/item_root";row.clickable=true;
   if(args[1].equals("tile_compact")){exact.text=null;exact.description=fileName+", 266 kB, 5:05 PM";exact.clickable=true;}
   else {
    AccessibilityNodeInfo child=exact;
    for(int depth=0;depth<4;depth++){AccessibilityNodeInfo parent=tasker.node(null,pkg,200);parent.children.add(child);child.parent=parent;child=parent;}
    row.children.add(child);child.parent=row;
   }
   if(args[1].equals("tile_unknown"))row.id=pkg+":id/nameplate";
   if(args[1].equals("tile_not_clickable"))row.clickable=false;
   if(args[1].equals("tile_changed")){row.children.clear();row.children.add(tasker.node("other.jpg",pkg,50));}
   if(args[1].equals("tile_preview")){exact.text=null;exact.description="Preview the file "+fileName;exact.id=pkg+":id/preview_icon";}
   bsh.set("exact",exact);
   if(args[1].startsWith("file_click_")){
    if(args[1].equals("file_click_rejected"))row.clickAllowed=false;
    AccessibilityWindowInfo window=new AccessibilityWindowInfo();window.root=row;window.id=500;tasker.service.windows.clear();tasker.service.windows.add(window);
    boolean rejected=false;try{bsh.eval("mqTap(tasker.service,\"com.google.android.documentsui\",\"file\",tasker.fileName);");}catch(bsh.TargetError failure){rejected=true;}
    System.out.println(new JSONObject().put("rejected",rejected).put("clickRequests",row.clickRequests).put("gestureCalls",tasker.service.gestureCalls).put("trace",bsh.get("mqTapAttempts")));return;
   }
   JSONObject selection=new JSONObject();
   try {AccessibilityNodeInfo target=(AccessibilityNodeInfo)bsh.eval("mqFileTapTarget(exact,\"com.google.android.documentsui\",tasker.fileName);");selection.put("selectedX",target.x).put("id",target.id==null?"":target.id).put("rejected",false);}
   catch(bsh.TargetError failure){selection.put("rejected",true);}
   System.out.println(selection);return;
  }
  if(args[1].startsWith("focus_")){
   AccessibilityNodeInfo input=tasker.node(null,"com.google.android.documentsui",0);input.editable=true;input.focused=args[1].equals("focus_existing");input.focusAllowed=!args[1].equals("focus_rejected");bsh.set("input",input);
   boolean rejected=false;try{bsh.eval("mqFocusSearch(input);");}catch(bsh.TargetError failure){rejected=true;}
   System.out.println(new JSONObject().put("focused",input.focused).put("focusRequests",input.focusRequests).put("rejected",rejected));return;
  }
  if(args[1].startsWith("selector_")){
   AccessibilityNodeInfo root=((AccessibilityWindowInfo)tasker.service.windows.get(0)).root;root.children.clear();
   AccessibilityNodeInfo n=tasker.node(null,"com.google.android.documentsui",0);
   n.description=args[1].equals("selector_preview")?"Preview the file "+fileName:fileName+", 266 kB, 5:05 PM";
   if(args[1].equals("selector_editable")){n.text=fileName;n.editable=true;}
   if(args[1].equals("selector_wrong_package"))n.pkg="other.package";
   root.children.add(n);
   if(args[1].equals("selector_nested")){AccessibilityNodeInfo child=tasker.node(fileName,n.pkg,5);child.parent=n;n.children.add(child);}
   if(args[1].equals("selector_duplicate")){AccessibilityNodeInfo n2=tasker.node(fileName,n.pkg,70);root.children.add(n2);}
   Object matches=bsh.eval("mqFind(tasker.service,\"com.google.android.documentsui\",\"file\",tasker.fileName).size();");
   System.out.println(new JSONObject().put("matches",matches));return;
  }
  JSONObject result=new JSONObject((String)bsh.eval(entry));
  if(args[1].startsWith("attach_")||args[1].equals("selection_uncertain")||args[1].equals("preview_timeout")){
   if(!result.getString("status").equals("prepared"))throw new AssertionError(result.toString());
   item.put("state","sending");tasker.vars.put("ManfredEyevueQueue",queue.toString());
   tasker.vars.put("mq_stage","attach_send");tasker.vars.put("mq_status","send_permitted");
   if(args[1].equals("attach_missing_stamp"))tasker.vars.remove("ManfredEyevueUiLedger");
   if(args[1].equals("attach_before_selection_offline"))android.net.NetworkCapabilities.validated=false;
   if(args[1].startsWith("attach_normalize_")||args[1].startsWith("attach_focus_")||args[1].equals("attach_poor_connection")||args[1].equals("attach_enabled")||args[1].equals("attach_resume_failed")||args[1].equals("attach_error_ui")||args[1].equals("attach_network_lost")||args[1].equals("attach_old_image")||args[1].equals("attach_delayed_new_image"))tasker.vars.put("ManfredEyevueUiConfirmed","verified_on_device_v1");
   result=new JSONObject((String)bsh.eval(entry));
   if(args[1].equals("attach_replay")){int count=tasker.taps.size();tasker.vars.put("mq_status","send_permitted");result=new JSONObject((String)bsh.eval(entry));if(tasker.taps.size()!=count)throw new AssertionError("Replay clicked");}
  }
  result.put("testTaps",new JSONArray(tasker.taps));result.put("testReads",context.resolver.reads);result.put("testBacks",tasker.service.globalBacks);
  result.put("testLedger",tasker.vars.get("ManfredEyevueUiLedger"));
  System.out.println(result);
 }
}
'''


class WorkerRuntimeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cache = Path(os.environ.get("MANFRED_TASKER_JAVA_CACHE", "/nonexistent"))
        java = list(cache.glob("*/bin/java.exe" if os.name == "nt" else "*/bin/java"))
        if len(java) != 1 or any(not (cache / x).is_file() for x in ("bsh.jar", "json.jar", "ecj.jar")):
            raise unittest.SkipTest("Set MANFRED_TASKER_JAVA_CACHE to the isolated verification runtime")
        cls.java = str(java[0])
        cls.tmp = tempfile.TemporaryDirectory(prefix="eyevue-worker-test-")
        root = Path(cls.tmp.name)
        sources = []
        for name, source in dict(STUBS, **{"WorkerHarness.java": DRIVER}).items():
            target = root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(source)
            sources.append(str(target))
        cls.cp = os.pathsep.join(str(x) for x in (root, cache / "bsh.jar", cache / "json.jar"))
        run = subprocess.run([cls.java, "-jar", str(cache / "ecj.jar"), "-nowarn", "-source", "1.8", "-target", "1.8", "-cp", cls.cp, "-d", str(root)] + sources, capture_output=True, text=True)
        if run.returncode:
            raise AssertionError(run.stdout + run.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def run_case(self, name):
        run = subprocess.run([self.java, "-Dmq.test.tmpdir=" + self.tmp.name, "-cp", self.cp, "WorkerHarness", str(WORKER), name], capture_output=True, text=True, timeout=20)
        self.assertEqual(0, run.returncode, run.stdout + run.stderr)
        result = json.loads(run.stdout)
        if not name.startswith(("selector_", "focus_", "tile_", "file_click_", "send_click_")):
            saved = json.loads((Path(self.tmp.name) / "Tasker/manfred-ui-worker.json").read_text())
            self.assertEqual(result["status"], saved["status"])
            self.assertEqual(result["reason"], saved["reason"])
            self.assertNotIn("UNRELATED PRIVATE TITLE", json.dumps(saved))
        return result

    def test_missing_gate_does_not_read_content_or_touch_ui(self):
        r = self.run_case("no_gate")
        self.assertEqual("no_op", r["status"])
        self.assertEqual([], r["testTaps"])
        self.assertEqual(0, r["testReads"])

    def test_receipt_and_owner_failures_never_touch_ui(self):
        for case in ("wrong_owner", "local_mismatch", "metadata_mismatch", "bytes_mismatch", "dimensions_mismatch"):
            with self.subTest(case=case):
                r = self.run_case(case)
                self.assertEqual("error", r["status"])
                self.assertEqual([], r["testTaps"])
                self.assertFalse(r["selectionAttempted"])

    def test_prepare_opens_picker_without_selecting_photo(self):
        r = self.run_case("prepare")
        self.assertEqual("prepared", r["status"])
        self.assertEqual(["Attachment", "Files", "Upload files"], r["testTaps"])
        self.assertFalse(r["selectionAttempted"])
        self.assertFalse(r["sendAttempted"])

    def test_existing_attachment_is_not_overwritten(self):
        r = self.run_case("existing_draft")
        self.assertEqual("error", r["status"])
        self.assertEqual("existing_attachment_draft", r["reason"])
        self.assertEqual([], r["testTaps"])

    def test_attach_needs_matching_prepared_stamp(self):
        r = self.run_case("attach_missing_stamp")
        self.assertEqual("error", r["status"])
        self.assertFalse(r["selectionAttempted"])
        self.assertEqual(3, len(r["testTaps"]))

    def test_selection_uncertainty_never_retries_or_sends(self):
        for case in ("selection_uncertain", "preview_timeout"):
            with self.subTest(case=case):
                r = self.run_case(case)
                self.assertEqual("ambiguous", r["status"])
                self.assertTrue(r["selectionAttempted"])
                self.assertFalse(r["sendAttempted"])
                self.assertEqual(4, len(r["testTaps"]))

    def test_stale_permit_cannot_repeat_attachment(self):
        r = self.run_case("attach_replay")
        self.assertEqual("error", r["status"])
        self.assertEqual(5, len(r["testTaps"]))

    def test_unverified_ui_submission_holds_by_default(self):
        r = self.run_case("attach_default")
        self.assertEqual("ambiguous", r["status"])
        self.assertTrue(r["submissionObserved"])
        self.assertTrue(r["selectionActionCompleted"])
        self.assertFalse(r["selectionGestureCompleted"])
        self.assertEqual("accessibility_click", r["selectionMethod"])
        self.assertTrue(r["sendActionCompleted"])
        self.assertFalse(r["sendGestureCompleted"])
        self.assertEqual("accessibility_click", r["sendMethod"])
        self.assertEqual(5, len(r["testTaps"]))

    def test_verified_ui_predicate_can_complete_without_claiming_server_receipt(self):
        r = self.run_case("attach_enabled")
        self.assertEqual("send_confirmed", r["status"])
        self.assertTrue(r["newImageObserved"])
        self.assertTrue(r["networkValidated"])
        self.assertFalse(r["voiceNeedsResume"])
        self.assertTrue(r["voiceResumed"])
        self.assertFalse(r["voiceActiveAfterSubmission"])
        self.assertEqual("unverified", r["voiceContinuity"])
        self.assertIn("no_server_receipt", r["reason"])

    def test_unchanged_old_image_or_changed_bounds_cannot_confirm_submission(self):
        r = self.run_case("attach_old_image")
        self.assertEqual("ambiguous", r["status"])
        self.assertFalse(r["newImageObserved"])
        self.assertTrue(r["confirmationEnabled"])
        self.assertGreaterEqual(r["elapsedMs"], 15000)
        self.assertLess(r["elapsedMs"], 16500)
        self.assertEqual(1, r["testTaps"].count("Send Message"))

    def test_error_or_lost_validated_network_prevents_completion(self):
        for case in ("attach_error_ui", "attach_network_lost"):
            with self.subTest(case=case):
                r = self.run_case(case)
                self.assertEqual("ambiguous", r["status"])
                self.assertEqual(5, len(r["testTaps"]))
                self.assertTrue(r["confirmationEnabled"])

    def test_exact_documentsui_grid_description_is_located(self):
        self.assertEqual(1, self.run_case("selector_grid")["matches"])

    def test_preview_search_field_and_other_package_are_excluded(self):
        for case in ("selector_preview", "selector_editable", "selector_wrong_package"):
            with self.subTest(case=case):
                self.assertEqual(0, self.run_case(case)["matches"])

    def test_prepare_waits_for_network_before_operating_ui(self):
        r = self.run_case("prepare_network_returns")
        self.assertEqual("prepared", r["status"])
        self.assertGreaterEqual(r["elapsedMs"], 900)
        self.assertEqual(["Attachment", "Files", "Upload files"], r["testTaps"])

    def test_prepare_network_timeout_or_lock_never_operates_ui(self):
        for case, reason in (("prepare_offline", "internet_not_validated_before_prepare"), ("phone_locked", "phone_locked")):
            with self.subTest(case=case):
                r = self.run_case(case)
                self.assertEqual("error", r["status"])
                self.assertEqual(reason, r["reason"])
                self.assertEqual([], r["testTaps"])
                self.assertFalse(r["selectionAttempted"])
                if case == "prepare_offline":
                    self.assertGreaterEqual(r["elapsedMs"], 20000)
                    self.assertLess(r["elapsedMs"], 21000)

    def test_lost_network_before_selection_does_not_consume_upload_attempt(self):
        r = self.run_case("attach_before_selection_offline")
        self.assertEqual("internet_not_validated_before_selection", r["reason"])
        self.assertFalse(r["selectionAttempted"])
        self.assertEqual(3, len(r["testTaps"]))
        self.assertEqual("prepared", json.loads(r["testLedger"])["phase"])

    def test_voice_resume_failure_does_not_undo_submission_or_repeat_send(self):
        r = self.run_case("attach_resume_failed")
        self.assertEqual("send_confirmed", r["status"])
        self.assertTrue(r["voiceResumeAttempted"])
        self.assertFalse(r["voiceResumed"])
        self.assertTrue(r["voiceNeedsResume"])
        self.assertEqual(1, r["testTaps"].count("Send Message"))
        self.assertEqual(1, r["testTaps"].count("Start a voice conversation"))
        self.assertEqual("ui_confirmed", json.loads(r["testLedger"])["phase"])

    def test_post_send_focus_change_never_reveals_history_or_confirms(self):
        r = self.run_case("attach_focus_success")
        self.assertEqual("ambiguous", r["status"])
        self.assertEqual("confirmation_view_changed", r["reason"])
        self.assertTrue(r["confirmationViewChanged"])
        self.assertFalse(r["newImageObserved"])
        self.assertFalse(r["focusModeToggleAttempted"])
        self.assertNotIn("toggle focus mode", r["testTaps"])
        self.assertEqual(1, r["testTaps"].count("Send Message"))

    def test_focus_normalization_precedes_selection_for_large_and_unknown_small_orbs(self):
        for case in ("attach_normalize_small", "attach_normalize_large"):
            with self.subTest(case=case):
                r = self.run_case(case)
                self.assertEqual("send_confirmed", r["status"])
                self.assertTrue(r["preSelectionFocusToggleCompleted"])
                self.assertTrue(r["presentationVerifiedBeforeSend"])
                self.assertFalse(r["confirmationViewChanged"])
                self.assertEqual("toggle focus mode", r["testTaps"][0])
                self.assertEqual(1, r["testTaps"].count("toggle focus mode"))
                self.assertFalse(r["focusModeToggleAttempted"])

    def test_unknown_small_orb_and_empty_transcript_stop_before_selecting(self):
        for case in ("prepare_unknown_small", "prepare_empty_transcript"):
            with self.subTest(case=case):
                r = self.run_case(case)
                self.assertEqual("error", r["status"])
                self.assertEqual("transcript_not_identified_before_selection", r["reason"])
                self.assertFalse(r["selectionAttempted"])
                self.assertFalse(r["sendAttempted"])
                self.assertNotIn("Attachment", r["testTaps"])
                if case == "prepare_unknown_small":
                    self.assertEqual("unknown_small_orb", r["focusedModeClassification"])
                    self.assertEqual(["toggle focus mode"], r["testTaps"])

    def test_inactive_voice_is_started_before_transcript_verification(self):
        r = self.run_case("prepare_voice_inactive")
        self.assertEqual("prepared", r["status"])
        self.assertEqual("Start a voice conversation", r["testTaps"][0])
        self.assertTrue(r["transcriptPrepared"])
        self.assertFalse(r["selectionAttempted"])

    def test_changed_presentation_after_picker_return_prevents_send(self):
        r = self.run_case("attach_presentation_changed_before_send")
        self.assertEqual("ambiguous", r["status"])
        self.assertEqual("transcript_changed_before_send", r["reason"])
        self.assertTrue(r["selectionAttempted"])
        self.assertFalse(r["sendAttempted"])
        self.assertNotIn("Send Message", r["testTaps"])

    def test_history_revealed_before_selection_is_not_a_new_submission(self):
        r = self.run_case("attach_normalize_history_no_new")
        self.assertEqual("ambiguous", r["status"])
        self.assertTrue(r["preSelectionFocusToggleCompleted"])
        self.assertEqual(1, len(r["imagesBeforeSend"]))
        self.assertEqual(1, len(r["imagesAfterSend"]))
        self.assertFalse(r["newImageObserved"])
        self.assertEqual(1, r["testTaps"].count("Send Message"))

    def test_another_windows_controls_do_not_establish_transcript(self):
        r = self.run_case("prepare_controls_other_window")
        self.assertEqual("transcript_not_identified_before_selection", r["reason"])
        self.assertFalse(r["selectionAttempted"])
        self.assertEqual([], r["testTaps"])

    def test_new_window_cannot_confirm_an_image_against_old_window_baseline(self):
        r = self.run_case("attach_new_window")
        self.assertEqual("confirmation_view_changed", r["reason"])
        self.assertFalse(r["newImageObserved"])
        self.assertTrue(r["confirmationViewChanged"])
        self.assertNotIn("toggle focus mode", r["testTaps"])

    def test_small_orb_does_not_hide_transcript_while_waiting_for_new_image(self):
        r = self.run_case("attach_focus_small_wait")
        self.assertEqual("send_confirmed", r["status"])
        self.assertEqual("transcript_small_orb", r["focusedModeClassification"])
        self.assertFalse(r["focusModeToggleAttempted"])
        self.assertNotIn("toggle focus mode", r["testTaps"])
        self.assertGreaterEqual(r["elapsedMs"], 2000)
        self.assertTrue(r["newImageObserved"])
        evidence = r["focusModeEvidence"]
        self.assertTrue(evidence["sameWindowVoice"])
        self.assertEqual("350 1500 650 1800", evidence["orbBounds"])
        self.assertEqual("0 0 1000 2000", evidence["windowBounds"])

    def test_small_orb_without_image_holds_without_blind_toggle(self):
        r = self.run_case("attach_focus_small_empty")
        self.assertEqual("ambiguous", r["status"])
        self.assertFalse(r["newImageObserved"])
        self.assertEqual("unknown_small_orb", r["focusedModeClassification"])
        self.assertFalse(r["focusModeToggleAttempted"])
        self.assertNotIn("toggle focus mode", r["testTaps"])
        self.assertTrue(r["confirmationViewChanged"])

    def test_large_orb_with_transcript_controls_is_conflicting_and_never_toggled(self):
        r = self.run_case("attach_focus_conflict")
        self.assertEqual("ambiguous", r["status"])
        self.assertEqual("conflicting_large_orb_with_transcript_controls", r["focusedModeClassification"])
        self.assertEqual(["Copy"], r["focusModeEvidence"]["transcriptControls"])
        self.assertFalse(r["focusModeToggleAttempted"])
        self.assertNotIn("toggle focus mode", r["testTaps"])

    def test_exact_disabled_image_is_diagnostic_only_and_cannot_confirm(self):
        r = self.run_case("attach_focus_disabled_image")
        self.assertEqual("ambiguous", r["status"])
        self.assertFalse(r["newImageObserved"])
        evidence = r["imageLocatorDiagnostic"]
        self.assertEqual(0, evidence["acceptedMatches"])
        self.assertEqual(1, evidence["exactLabelMatches"])
        self.assertFalse(evidence["eligibility"][0]["enabled"])
        self.assertTrue(evidence["eligibility"][0]["visible"])
        self.assertNotIn("toggle focus mode", r["testTaps"])

    def test_uploading_attachment_never_completes_even_with_confirmation_enabled(self):
        r = self.run_case("attach_focus_uploading")
        self.assertEqual("ambiguous", r["status"])
        self.assertEqual("upload_in_progress", r["reason"])
        self.assertTrue(r["uploadInProgress"])
        self.assertTrue(r["newImageObserved"])
        self.assertTrue(r["confirmationEnabled"])
        self.assertEqual(0, r["testTaps"].count("toggle focus mode"))
        self.assertNotIn("Retry", r["testTaps"])

    def test_upload_must_clear_before_ui_completion(self):
        r = self.run_case("attach_focus_upload_clears")
        self.assertEqual("send_confirmed", r["status"])
        self.assertTrue(r["uploadObserved"])
        self.assertFalse(r["uploadInProgress"])
        self.assertEqual(1, r["testTaps"].count("Send Message"))

    def test_observed_connection_unsent_and_multiple_retry_controls_hold_without_retry(self):
        for case in ("attach_focus_unsent", "attach_poor_connection"):
            with self.subTest(case=case):
                r = self.run_case(case)
                self.assertEqual("ambiguous", r["status"])
                self.assertEqual("upload_error_observed", r["reason"])
                self.assertTrue(r["errorUiObserved"])
                self.assertNotIn("Retry", r["testTaps"])
                self.assertEqual(1, r["testTaps"].count("Send Message"))

    def test_send_waits_for_transient_parent_readiness_before_its_single_action(self):
        r = self.run_case("attach_send_delayed_ready")
        self.assertTrue(r["sendAttempted"])
        self.assertTrue(r["sendActionCompleted"])
        self.assertGreaterEqual(r["sendTarget"]["readinessChecks"], 4)
        self.assertEqual(1, r["testTaps"].count("Send Message"))

    def test_send_readiness_timeout_records_ancestors_without_consuming_send_attempt(self):
        r = self.run_case("attach_send_never_ready")
        self.assertEqual("ambiguous", r["status"])
        self.assertEqual("send_clickable_parent_not_identified", r["reason"])
        self.assertTrue(r["selectionAttempted"])
        self.assertFalse(r["sendAttempted"])
        self.assertFalse(r["sendActionCompleted"])
        self.assertNotIn("Send Message", r["testTaps"])
        self.assertEqual("selection_attempted", json.loads(r["testLedger"])["phase"])
        self.assertGreaterEqual(r["elapsedMs"], 5000)
        self.assertLess(r["elapsedMs"], 6500)
        target = r["sendTarget"]
        self.assertTrue(target["timedOut"])
        self.assertEqual(1, target["windowId"])
        self.assertEqual(3, len(target["ancestors"]))
        parent = target["ancestors"][1]
        self.assertEqual("android.view.View", parent["class"])
        self.assertEqual("com.openai.chatgpt", parent["package"])
        self.assertTrue(parent["visible"])
        self.assertTrue(parent["enabled"])
        self.assertFalse(parent["clickable"])
        self.assertFalse(parent["editable"])
        self.assertEqual([], parent["actionIds"])
        self.assertEqual("500 10 550 60", parent["bounds"])
        self.assertNotIn("text", parent)
        self.assertNotIn("description", parent)

    def test_observation_keeps_waiting_when_only_the_old_image_is_initially_visible(self):
        r = self.run_case("attach_delayed_new_image")
        self.assertEqual("send_confirmed", r["status"])
        self.assertTrue(r["newImageObserved"])
        self.assertEqual(1, len(r["imagesBeforeSend"]))
        self.assertEqual(2, len(r["imagesAfterSend"]))
        self.assertGreaterEqual(r["elapsedMs"], 1800)
        self.assertEqual(1, r["testTaps"].count("Send Message"))

    def test_send_uses_exact_isolated_clickable_parent_once(self):
        r = self.run_case("send_click_accepted")
        self.assertFalse(r["rejected"])
        self.assertEqual(1, r["clickRequests"])
        self.assertEqual(0, r["composerClicks"])
        self.assertEqual(0, r["gestureCalls"])
        self.assertEqual(1, len(r["trace"]))
        tap = r["trace"][0]
        self.assertEqual("50 10 100 60", tap["bounds"])
        self.assertEqual([16], tap["actionIds"])
        self.assertEqual("accessibility_click", tap["method"])
        self.assertEqual("send", tap["actionRole"])
        self.assertTrue(tap["nativeActionReturned"])
        self.assertFalse(tap["gestureCompleted"])

    def test_native_send_rejection_has_no_retry_or_gesture_fallback(self):
        r = self.run_case("send_click_rejected")
        self.assertTrue(r["rejected"])
        self.assertEqual(1, r["clickRequests"])
        self.assertEqual(0, r["composerClicks"])
        self.assertEqual(0, r["gestureCalls"])
        self.assertFalse(r["trace"][0]["nativeActionReturned"])

    def test_send_rejects_nonisolated_duplicate_disabled_or_wrong_package_parent(self):
        for case in ("send_click_extra_label", "send_click_duplicate", "send_click_composer_only", "send_click_wrong_package", "send_click_disabled"):
            with self.subTest(case=case):
                r = self.run_case(case)
                self.assertTrue(r["rejected"])
                self.assertEqual(0, r["clickRequests"])
                self.assertEqual(0, r["composerClicks"])
                self.assertEqual(0, r["gestureCalls"])

    def test_file_tile_uses_one_native_click_and_records_real_target(self):
        r = self.run_case("file_click_accepted")
        self.assertFalse(r["rejected"])
        self.assertEqual(1, r["clickRequests"])
        self.assertEqual(0, r["gestureCalls"])
        self.assertEqual(1, len(r["trace"]))
        tap = r["trace"][0]
        self.assertEqual("com.google.android.documentsui:id/item_root", tap["targetId"])
        self.assertEqual("50 10 100 60", tap["bounds"])
        self.assertEqual([16], tap["actionIds"])
        self.assertEqual("accessibility_click", tap["method"])
        self.assertTrue(tap["nativeActionReturned"])
        self.assertFalse(tap["gestureCompleted"])

    def test_rejected_native_file_click_has_no_gesture_fallback(self):
        r = self.run_case("file_click_rejected")
        self.assertTrue(r["rejected"])
        self.assertEqual(1, r["clickRequests"])
        self.assertEqual(0, r["gestureCalls"])
        self.assertFalse(r["trace"][0]["nativeActionReturned"])
        self.assertFalse(r["trace"][0]["gestureCompleted"])

    def test_file_title_resolves_to_clickable_item_root_five_levels_up(self):
        r = self.run_case("tile_normal")
        self.assertFalse(r["rejected"])
        self.assertEqual("com.google.android.documentsui:id/item_root", r["id"])
        self.assertEqual(50, r["selectedX"])

    def test_compact_described_clickable_file_row_is_supported(self):
        r = self.run_case("tile_compact")
        self.assertFalse(r["rejected"])
        self.assertEqual(400, r["selectedX"])

    def test_preview_unknown_nonclickable_or_changed_file_tile_is_rejected(self):
        for case in ("tile_preview", "tile_unknown", "tile_not_clickable", "tile_changed"):
            with self.subTest(case=case):
                self.assertTrue(self.run_case(case)["rejected"])

    def test_known_initial_attachment_popup_is_dismissed_once_before_prepare(self):
        r = self.run_case("stale_popup")
        self.assertEqual("prepared", r["status"])
        self.assertTrue(r["initialPopupDismissed"])
        self.assertEqual(1, r["testBacks"])
        self.assertFalse(r["selectionAttempted"])
        self.assertEqual(["Attachment", "Files", "Upload files"], r["testTaps"])

    def test_unknown_popup_is_never_dismissed(self):
        r = self.run_case("unknown_popup")
        self.assertEqual("tap_target_occluded", r["reason"])
        self.assertEqual(0, r["testBacks"])
        self.assertEqual([], r["testTaps"])

    def test_initial_popup_recovery_does_not_touch_existing_draft(self):
        r = self.run_case("stale_popup_existing_draft")
        self.assertEqual("existing_attachment_draft", r["reason"])
        self.assertEqual(0, r["testBacks"])
        self.assertEqual([], r["testTaps"])

    def test_initial_popup_back_failure_or_timeout_is_not_retried(self):
        for case in ("stale_popup_back_denied", "stale_popup_back_stuck"):
            with self.subTest(case=case):
                r = self.run_case(case)
                self.assertEqual("error", r["status"])
                self.assertEqual(1, r["testBacks"])
                self.assertEqual([], r["testTaps"])
                self.assertFalse(r["selectionAttempted"])

    def test_occlusion_retains_exact_window_metadata_without_other_app_text(self):
        r = self.run_case("occluded")
        self.assertEqual("tap_target_occluded", r["reason"])
        self.assertFalse(r["selectionAttempted"])
        self.assertEqual([], r["testTaps"])
        block = r["tapBlock"]
        self.assertEqual("Attachment", block["target"]["selectorValue"])
        self.assertEqual(1, block["target"]["windowId"])
        self.assertEqual(8, block["obstruction"]["windowId"])
        self.assertEqual(3, block["obstruction"]["layer"])
        self.assertEqual(3, block["obstruction"]["type"])
        self.assertEqual("com.example.overlay", block["obstruction"]["rootPackage"])

    def test_search_requests_focus_before_submission(self):
        r = self.run_case("focus_needed")
        self.assertTrue(r["focused"])
        self.assertEqual(1, r["focusRequests"])
        self.assertFalse(r["rejected"])

    def test_search_does_not_refocus_an_already_focused_input(self):
        r = self.run_case("focus_existing")
        self.assertTrue(r["focused"])
        self.assertEqual(0, r["focusRequests"])

    def test_search_focus_denial_stops_before_submission(self):
        r = self.run_case("focus_rejected")
        self.assertFalse(r["focused"])
        self.assertTrue(r["rejected"])

    def test_filename_child_and_described_file_row_are_one_result(self):
        self.assertEqual(1, self.run_case("selector_nested")["matches"])

    def test_duplicate_filename_results_remain_ambiguous(self):
        self.assertEqual(2, self.run_case("selector_duplicate")["matches"])


if __name__ == "__main__":
    unittest.main()
